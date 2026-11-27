import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengReceipt, BlengFailure } from './BlengFailure.nitro'
import type { BlengEnd } from './BlengEnd.nitro'
import type { BlengGattDiscoverySubmission, BlengMtuSubmission, BlengRssiSubmission } from './BlengGatt.nitro'

/**
 * Owner-scoped connection capability created by Rust.
 *
 * It carries no address, handle or owner identity. Native code can only address a connection
 * through the object Rust returned for the engine that produced it.
 */
export interface BlengConnection extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  beginDisconnect(): BlengDisconnectSubmission
  /** One discovery per call; a later discovery yields a new session identity. */
  beginDiscoverGatt(): BlengGattDiscoverySubmission
  /** Connected RSSI is a bounded request/response, not a subscription. */
  beginReadRssi(): BlengRssiSubmission
  /** Requests an MTU where supported; the result reports what the platform actually did. */
  beginRequestMtu(mtu: number): BlengMtuSubmission
  /** Queries current MTU/payload limits without requesting native negotiation. */
  beginQueryMtu(): BlengMtuSubmission
  /**
   * Requests a link scheduling preference where the platform supports it.
   *
   * The receipt only records that the platform accepted the request: no platform reports the
   * effective scheduling back, so this is not a measurement of the link mode.
   */
  beginSetConnectionPriority(priority: BlengConnectionPriority): BlengPrioritySubmission
  /**
   * Requests bonding (pairing) where the platform supports it.
   *
   * Success means the platform reported `bonded`; it is not a promise that the link is encrypted or
   * that a later reconnect happens without user interaction. Apple has no programmatic bonding API,
   * so it refuses with `bleng.dispatch_rejected.not_supported` instead of pretending.
   *
   * `pin` is only the platform's numeric PIN pairing input (Android); omit it to leave the system
   * pairing UI in charge. A passkey/consent prompt is never auto-answered, and the PIN is dropped as
   * soon as the platform consumes it.
   */
  beginBond(pin?: string): BlengBondSubmission
  /**
   * Requests bond removal where the platform supports it.
   *
   * Success means the platform reported `none`. Apple has no public API to remove a bond, so it
   * refuses with `bleng.dispatch_rejected.not_supported`.
   */
  beginRemoveBond(): BlengBondSubmission
  /**
   * Requests invalidation of the platform's cached GATT topology.
   *
   * Android refreshes through the platform cache control; on success this connection's earlier
   * GATT sessions are stale and must be rediscovered. Apple refuses with
   * `bleng.dispatch_rejected.not_supported`.
   */
  beginGattCacheRefresh(): BlengGattCacheRefreshSubmission
  /**
   * Whether this connection's own lane currently sees the link as up.
   *
   * A connect still awaiting platform confirmation reports `false`, and so does an ended lane. It
   * is the engine's observation, not a fresh platform probe of the peer.
   */
  isConnected(): boolean
  /**
   * The peer's self-reported advertisement name, when the peripheral capability carried one.
   *
   * This is the name observed in the advertisement that produced the peripheral, not a fresh
   * platform read and not a claim about the live link. It is `undefined` when the peer advertised
   * no name and for a capability adopted from an inventory key.
   */
  localName(): string | undefined
  /**
   * The link's retained end, so a caller never has to send a command to learn the link is gone.
   *
   * At most one pull is pending. A deadline is the caller's own bound, not a statement about the
   * link: it reports a failure and the end stays readable afterwards. The end is readable more
   * than once — a caller that starts observing late still learns what happened.
   */
  awaitEnd(timeoutMs: number): Promise<BlengConnectionNext>
}

/** Original connect result; the value is single-consumer. */
export interface BlengConnect extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengConnectResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengDisconnect extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

/** What the caller asks the link to optimise for. The platform decides whether it applies it. */
export type BlengConnectionPriority = 'balanced' | 'high' | 'lowPower'

/** One bounded link-priority receipt. It carries no effective mode because none is reported. */
export interface BlengPriority extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

/** The platform-reported bond state. `bonding` is progress, not a terminal. */
export type BlengBondState = 'none' | 'bonding' | 'bonded'

/**
 * One platform-reported progress step.
 *
 * `state` is absent only on the retained end of the lane; `ended` is true only for the terminal
 * state or that end. Stop pulling after `ended`; the terminal receipt still comes from `wait`.
 */
export interface BlengBondProgress {
  state?: BlengBondState
  ended: boolean
}

/** One bounded bond (pairing) receipt; `state` is the platform's terminal report. */
export interface BlengBond extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengBondResult>
  cancel(): Promise<BlengReceipt>
  /**
   * Pulls the next platform-reported state (usually `bonding`). This is observation only: the
   * terminal receipt stays on `wait`, and a timeout is the caller's deadline, not a cancellation.
   */
  nextState(timeoutMs: number): Promise<BlengBondProgressResult>
}

export interface BlengBondAccepted {
  bond: BlengBond
}

export interface BlengBondOutcome {
  state: BlengBondState
}

/** Absence of failure is a submitted request; a bonded peer is the `bonded` result. */
export type BlengBondSubmission = BlengBondAccepted | BlengFailure
export type BlengBondResult = BlengBondOutcome | BlengFailure
export type BlengBondProgressResult = BlengBondProgress | BlengFailure

/** One bounded GATT-cache refresh receipt. */
export interface BlengGattCacheRefresh extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengGattCacheRefreshAccepted {
  refresh: BlengGattCacheRefresh
}

export type BlengGattCacheRefreshSubmission = BlengGattCacheRefreshAccepted | BlengFailure

export interface BlengPriorityAccepted {
  priority: BlengPriority
}

/** Absence of failure is an accepted request, not an applied scheduling mode. */
export type BlengPrioritySubmission = BlengPriorityAccepted | BlengFailure

export interface BlengConnectionEndedEvent {
  /** Reports `requested`, `unrequested` or `ownerClosed`. */
  ended: BlengEnd
}

export type BlengConnectionNext = BlengConnectionEndedEvent | BlengFailure

export interface BlengConnectAccepted {
  connect: BlengConnect
}

export interface BlengConnectOutcome {
  connection: BlengConnection
}

export interface BlengDisconnectAccepted {
  disconnect: BlengDisconnect
}

/** Absence of failure is a bounded submission, not an established or torn-down link. */
export type BlengConnectSubmission = BlengConnectAccepted | BlengFailure
export type BlengConnectResult = BlengConnectOutcome | BlengFailure
export type BlengDisconnectSubmission = BlengDisconnectAccepted | BlengFailure
