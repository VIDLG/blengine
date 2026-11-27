import type { HybridObject } from 'react-native-nitro-modules'
import type { BlengReceipt, BlengFailure } from './BlengFailure.nitro'
import type {
  BlengNotificationModeRequest, BlengSubscriptionSubmission,
} from './BlengSubscription.nitro'

export type BlengWriteType = 'withResponse' | 'withoutResponse'

/** Platform-reported properties, exactly as Rust returned them. */
export interface BlengCharacteristicProperties {
  readable: boolean
  writableWithResponse: boolean
  writableWithoutResponse: boolean
  notifiable: boolean
  indicatable: boolean
}

/**
 * An addressable descriptor. It belongs to exactly one discovered characteristic, so a later
 * discovery yields new descriptor capabilities instead of re-pointing the old ones.
 *
 * Descriptor writes always expect an ATT response; there is no write type.
 */
export interface BlengGattDescriptor extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  uuid(): string
  beginRead(): BlengDescriptorReadSubmission
  beginWrite(value: ArrayBuffer): BlengDescriptorWriteSubmission
}

export interface BlengDescriptorRead extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengDescriptorReadResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengDescriptorWrite extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengDescriptorReadAccepted {
  read: BlengDescriptorRead
}
export interface BlengDescriptorWriteAccepted {
  write: BlengDescriptorWrite
}
/** A descriptor value; `undefined` is distinct from a zero-length descriptor. */
export interface BlengDescriptorReadOutcome {
  value: ArrayBuffer
}

export type BlengDescriptorReadSubmission = BlengDescriptorReadAccepted | BlengFailure
export type BlengDescriptorReadResult = BlengDescriptorReadOutcome | BlengFailure
export type BlengDescriptorWriteSubmission = BlengDescriptorWriteAccepted | BlengFailure

/**
 * One discovered topology. Every attribute below belongs to exactly this session: a later
 * discovery yields a new session instead of re-pointing the old capabilities.
 */
export interface BlengGattSession extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  services(): BlengGattService[]
}

/**
 * Tri-state service kind. `unknown` means the platform did not report primary/secondary, which is
 * not the same as secondary.
 */
export type BlengServiceKind = 'primary' | 'secondary' | 'unknown'

export interface BlengGattService extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  uuid(): string
  kind(): BlengServiceKind
  characteristics(): BlengGattCharacteristic[]
}

/** An addressable characteristic; reads and writes are its only controls. */
export interface BlengGattCharacteristic extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  uuid(): string
  properties(): BlengCharacteristicProperties
  descriptors(): BlengGattDescriptor[]
  beginRead(): BlengReadSubmission
  beginWrite(value: ArrayBuffer, writeType: BlengWriteType): BlengWriteSubmission
  /** One bounded enable; its value stream is a separate bounded lane. */
  beginSubscription(
    mode: BlengNotificationModeRequest,
  ): BlengSubscriptionSubmission
}

export interface BlengGattDiscovery extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengGattDiscoveryResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengRead extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReadResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengWrite extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

/** Connected RSSI is a bounded request/response, not a subscription. */
export interface BlengRssi extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengRssiResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengMtuInfo {
  /** Absent when the platform does not expose the negotiated MTU (CoreBluetooth does not). */
  mtu?: number
  maximumWriteWithResponse?: number
  maximumWriteWithoutResponse?: number
}

/** One bounded MTU query/request. A submission is not a negotiated value. */
export interface BlengMtu extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengMtuResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengGattDiscoveryAccepted {
  discovery: BlengGattDiscovery
}
export interface BlengGattSessionOutcome {
  session: BlengGattSession
}
export interface BlengReadAccepted {
  read: BlengRead
}
export interface BlengWriteAccepted {
  write: BlengWrite
}
export interface BlengRssiAccepted {
  rssi: BlengRssi
}
export interface BlengMtuAccepted {
  mtu: BlengMtu
}
/** A read value; `undefined` is distinct from a zero-length value. */
export interface BlengReadOutcome {
  value: ArrayBuffer
}
/** Zero and negative RSSI are real values, so absence is `undefined`. */
export interface BlengRssiOutcome {
  rssi: number
}
export interface BlengMtuOutcome {
  info: BlengMtuInfo
}

/** Absence of failure is a bounded submission, not a discovered or completed operation. */
export type BlengGattDiscoverySubmission = BlengGattDiscoveryAccepted | BlengFailure
export type BlengGattDiscoveryResult = BlengGattSessionOutcome | BlengFailure
export type BlengReadSubmission = BlengReadAccepted | BlengFailure
export type BlengWriteSubmission = BlengWriteAccepted | BlengFailure
export type BlengReadResult = BlengReadOutcome | BlengFailure
export type BlengRssiSubmission = BlengRssiAccepted | BlengFailure
export type BlengRssiResult = BlengRssiOutcome | BlengFailure
export type BlengMtuSubmission = BlengMtuAccepted | BlengFailure
export type BlengMtuResult = BlengMtuOutcome | BlengFailure
