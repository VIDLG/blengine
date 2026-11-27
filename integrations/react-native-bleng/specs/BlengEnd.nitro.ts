import type { BlengFailureInfo } from './BlengFailure.nitro'

/**
 * Why a capability stopped being observable, in one vocabulary shared by every lane.
 *
 * Each lane reports only the subset it can actually distinguish; the value is a fact about
 * observation, never a claim about the native radio, link or subscription itself.
 *
 * - `requested` — the caller asked this capability to end it (stop, disable, disconnect).
 * - `unrequested` — the link or peer ended it without this capability asking.
 * - `failed` — the platform reported a failure.
 * - `systemSuspended` — the platform suspended scanning (Android).
 * - `connectionClosed` — a subscription lane lost the link it belonged to.
 * - `ownerClosed` — the engine was retired; observation ended, the radio is not implied off.
 * - `detached` — this endpoint stopped observing; other observers keep running.
 * - `overflowed` — a bounded lane buffer overflowed and the lane failed closed.
 */
export type BlengEndReason =
  | 'requested'
  | 'unrequested'
  | 'failed'
  | 'systemSuspended'
  | 'connectionClosed'
  | 'ownerClosed'
  | 'detached'
  | 'overflowed'

/** The one end record every lane reports. `failure` is present only when the end was a failure. */
export interface BlengEnd {
  reason: BlengEndReason
  failure?: BlengFailureInfo
}
