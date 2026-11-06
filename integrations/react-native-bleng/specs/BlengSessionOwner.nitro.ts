import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengScanOptions, BlengScanSubmission } from './BlengScan.nitro'

export type BlengAdapterState = 'unknown' | 'unavailable' | 'poweredOff' | 'poweredOn'

export type BlengAuthorizationState =
  | 'notRequired'
  | 'notDetermined'
  | 'denied'
  | 'restricted'
  | 'authorized'

/** Initialization evidence, not a live subscription or a promise of BLE availability. */
export interface BlengAdapterSnapshot {
  state: BlengAdapterState
  scanAuthorization: BlengAuthorizationState
  connectionAuthorization: BlengAuthorizationState
  name?: string
}

/** A stable, identity-free failure; detailed native diagnostics stay on the native lane. */
export interface BlengCloseFailure {
  code: string
  message: string
}

/** Owner closure is independent of cleanup success; neither a failure nor a closed owner proves resource-zero. */
export interface BlengCloseResult {
  ownerClosed: boolean
  failure?: BlengCloseFailure
}

/**
 * Apple/Android typed session bridge backed by the Rust Supervisor.
 *
 * Start returns the actual initialization snapshot even when radio is off or access is denied.
 * No raw Bleng object, backend handle, operation ID or recovery context/plan crosses into JS.
 * Scan control/results delegate to Rust; observations carry opaque peripheral capabilities.
 */
export interface BlengSessionOwner extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  start(): Promise<BlengAdapterSnapshot>
  beginScan(options: BlengScanOptions): BlengScanSubmission
  close(): Promise<BlengCloseResult>
}
