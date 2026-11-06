import type { HybridObject } from 'react-native-nitro-modules'

export type BlengScanFailureDomain =
  | 'input' | 'submission' | 'operation' | 'observation' | 'delivery' | 'lifecycle'
// Diagnostic platform names are data, not control-state enums. A string also avoids the NDK's
// ANDROID macro colliding with Nitrogen's generated C++ enum member.
export type BlengScanPlatform = string

export interface BlengScanPlatformDetails {
  platform: BlengScanPlatform
  code?: string
  name?: string
  message?: string
}

/** Classified in Rust; code is stable, while message is for display, never control flow. */
export interface BlengScanFailureInfo {
  domain: BlengScanFailureDomain
  code: string
  message: string
  platform?: BlengScanPlatformDetails
}

export interface BlengScanFailure {
  failure: BlengScanFailureInfo
}

/** Absence of failure means this call succeeded; cancel is a receipt, not remote rollback. */
export interface BlengScanCommandResult {
  failure?: BlengScanFailureInfo
}

export interface BlengScanOptions {
  serviceUuids: string[]
  namePrefix?: string
  companyId?: number
  allowDuplicates: boolean
}

export interface BlengManufacturerData {
  companyId: number
  payload: ArrayBuffer
}

export interface BlengServiceData {
  serviceUuid: string
  payload: ArrayBuffer
}

/** Native-created owner-bound capability, not a device ID or a connected device. */
export interface BlengPeripheral extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {}

export interface BlengScanAdvertisement {
  peripheral: BlengPeripheral
  localName?: string
  advertisedServices: string[]
  manufacturerData: BlengManufacturerData[]
  serviceData: BlengServiceData[]
  rssi?: number
  connectable?: boolean
}

export interface BlengScanAdvertisementEvent {
  advertisement: BlengScanAdvertisement
}

export type BlengScanEndReason = 'requested' | 'systemSuspended' | 'failed' | 'ownerClosed'
export interface BlengScanEnd {
  reason: BlengScanEndReason
  failure?: BlengScanFailureInfo
}
export interface BlengScanEndedEvent {
  ended: BlengScanEnd
}

// Distinct required keys are representable by the pinned Nitrogen record union.
export type BlengScanNext = BlengScanAdvertisementEvent | BlengScanEndedEvent | BlengScanFailure

export interface BlengScanStop extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengScanCommandResult>
  cancel(): Promise<BlengScanCommandResult>
}

export interface BlengScan extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Single-consumer original start result; timeout does not stop native scanning. */
  waitUntilStarted(timeoutMs: number): Promise<BlengScanCommandResult>
  cancelStart(): Promise<BlengScanCommandResult>
  /** At most one pending pull; caller consumption never blocks native control. */
  nextEvent(timeoutMs: number): Promise<BlengScanNext>
  beginStop(): BlengScanStopSubmission
}

export interface BlengScanAccepted {
  scan: BlengScan
}
export interface BlengScanStopAccepted {
  operation: BlengScanStop
}
export type BlengScanSubmission = BlengScanAccepted | BlengScanFailure
export type BlengScanStopSubmission = BlengScanStopAccepted | BlengScanFailure
