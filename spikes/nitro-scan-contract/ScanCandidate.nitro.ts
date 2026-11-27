import type { HybridObject } from 'react-native-nitro-modules'

// Code-generation experiment only. None of these types is exported by the formal package.
export type ScanFailureDomain = 'submission' | 'operation' | 'delivery' | 'lifecycle'
export type ScanPlatform = 'apple' | 'android'

export interface ScanPlatformDetails {
  platform: ScanPlatform
  code?: string
  name?: string
  message?: string
}

export interface ScanFailureInfo {
  domain: ScanFailureDomain
  code: string
  message: string
  platform?: ScanPlatformDetails
}

export interface ScanFailure {
  failure: ScanFailureInfo
}

/** No failure means this call succeeded; wait results and cancellation receipts stay distinct. */
export interface ScanCommandResult {
  failure?: ScanFailureInfo
}

export interface ScanOptionsCandidate {
  serviceUuids: string[]
  namePrefix?: string
  companyId?: number
  allowDuplicates: boolean
}

export interface ScanManufacturerData {
  companyId: number
  payload: ArrayBuffer
}

export interface ScanServiceData {
  serviceUuid: string
  payload: ArrayBuffer
}

/** Opaque owner-bound capability; stopping a scan does not retire its peripheral handles. */
export interface ScanPeripheralCandidate extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {}

export interface ScanAdvertisement {
  peripheral: ScanPeripheralCandidate
  localName?: string
  advertisedServices: string[]
  manufacturerData: ScanManufacturerData[]
  serviceData: ScanServiceData[]
  rssi?: number
  connectable?: boolean
}

export interface ScanAdvertisementEvent {
  advertisement: ScanAdvertisement
}

export type ScanEndReasonCandidate = 'requested' | 'systemSuspended' | 'failed' | 'ownerClosed'

export interface ScanEndCandidate {
  reason: ScanEndReasonCandidate
  failure?: ScanFailureInfo
}

export interface ScanEndedEvent {
  ended: ScanEndCandidate
}

// Distinct required keys avoid unsupported singleton string-literal discriminants.
export type ScanNextCandidate = ScanAdvertisementEvent | ScanEndedEvent | ScanFailure

/** Original typed stop result and cancellation, not a second native operation state machine. */
export interface ScanStopCandidate extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Original stop result; success is not a JS event-reader drain or owner resource-zero. */
  wait(timeoutMs: number): Promise<ScanCommandResult>
  /** Cancellation receipt only, not evidence that the stop operation succeeded. */
  cancel(): Promise<ScanCommandResult>
}

export interface ScanSessionCandidate extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Original start result; observation timeout does not stop the native scan. */
  waitUntilStarted(timeoutMs: number): Promise<ScanCommandResult>
  /** Cancellation receipt only, not evidence that the scan started or stopped. */
  cancelStart(): Promise<ScanCommandResult>
  nextEvent(timeoutMs: number): Promise<ScanNextCandidate>
  beginStop(): ScanStopSubmissionCandidate
}

export interface ScanAcceptedCandidate {
  scan: ScanSessionCandidate
}

export interface ScanStopAcceptedCandidate {
  operation: ScanStopCandidate
}

export type ScanSubmissionCandidate = ScanAcceptedCandidate | ScanFailure
export type ScanStopSubmissionCandidate = ScanStopAcceptedCandidate | ScanFailure

/** Native-only contract proposal, deliberately separate from BlengSessionOwner.nitro.ts. */
export interface ScanOwnerCandidate extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  beginScan(options: ScanOptionsCandidate): ScanSubmissionCandidate
}
