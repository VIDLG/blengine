import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengReceipt, BlengFailure } from './BlengFailure.nitro'
import type { BlengEnd } from './BlengEnd.nitro'
import type { BlengConnectSubmission } from './BlengConnection.nitro'

export interface BlengScanOptions {
  serviceUuids: string[]
  namePrefix?: string
  exactName?: string
  companyId?: number
  minRssi?: number
  /** Match one manufacturer payload by prefix, or byte-for-byte under `mask`. */
  manufacturerData?: BlengManufacturerDataFilter
  /**
   * Android-only native scanner tuning. Apple refuses a scan that sets it with
   * `bleng.dispatch_rejected.not_supported` instead of scanning with different behaviour.
   */
  android?: BlengAndroidScanOptions
  allowDuplicates: boolean
}

export type BlengAndroidScanMode =
  | 'lowPower'
  | 'balanced'
  | 'lowLatency'
  | 'opportunistic'

export type BlengAndroidScanPhy = 'le1m' | 'le2m' | 'leCoded'

/** Scanner hints, not match filters: they change how Android scans, never what matches. */
export interface BlengAndroidScanOptions {
  scanMode?: BlengAndroidScanMode
  /** Requires API 26. */
  phy?: BlengAndroidScanPhy
  /** Batch delay in ms; `0` reports every result immediately. */
  reportDelayMs?: number
  /** Requires API 27. */
  legacy?: boolean
}

export interface BlengManufacturerDataFilter {
  companyId: number
  data: ArrayBuffer
  mask?: ArrayBuffer
}

export interface BlengManufacturerData {
  companyId: number
  payload: ArrayBuffer
}

export interface BlengServiceData {
  serviceUuid: string
  payload: ArrayBuffer
}

/**
 * Native-created owner-bound capability, not a device ID or a connected device.
 *
 * Connecting is the only control a scanned observation offers; the resulting connection is again
 * a Rust-created capability.
 */
export interface BlengPeripheral extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /**
   * Opaque and owner-scoped: stable across advertisements of the same peer, useful only as a map
   * key. It is not a platform device id and cannot address native state.
   */
  key(): string
  /**
   * Local name from the advertisement that produced this capability.
   *
   * `null` when the peer advertised no name, and for a capability adopted from an inventory key
   * before any new observation. It is the peer's self-reported name, never a platform device name
   * and never a claim about a live link.
   */
  localName(): string | undefined
  /** One-shot connect attempt. */
  beginConnect(): BlengConnectSubmission
  /**
   * Asks the platform to establish the link whenever the peer becomes available.
   *
   * Android can express this natively. Apple has no equivalent, so it refuses with
   * `bleng.dispatch_rejected.not_supported` instead of silently connecting once.
   */
  beginAutoConnect(): BlengConnectSubmission
}

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

export interface BlengScanEndedEvent {
  /** Reports `requested`, `systemSuspended`, `failed`, `ownerClosed` or `overflowed`. */
  ended: BlengEnd
}

// Distinct required keys are representable by the pinned Nitrogen record union.
export type BlengScanNext = BlengScanAdvertisementEvent | BlengScanEndedEvent | BlengFailure

export interface BlengScanStop extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengScan extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Single-consumer original start result; timeout does not stop native scanning. */
  waitUntilStarted(timeoutMs: number): Promise<BlengReceipt>
  cancelStart(): Promise<BlengReceipt>
  /**
   * Whether this lane has observed a confirmed start that has not ended.
   *
   * `false` means the engine does not know a scan is active (start unconfirmed, or a terminal was
   * observed, including this lane's own overflow). It never claims the native scan has stopped.
   */
  isScanning(): boolean
  /** At most one pending pull; caller consumption never blocks native control. */
  nextEvent(timeoutMs: number): Promise<BlengScanNext>
  beginStop(): BlengScanStopSubmission
}

export interface BlengScanAccepted {
  scan: BlengScan
}
export interface BlengScanStopAccepted {
  stop: BlengScanStop
}
export type BlengScanSubmission = BlengScanAccepted | BlengFailure
export type BlengScanStopSubmission = BlengScanStopAccepted | BlengFailure
