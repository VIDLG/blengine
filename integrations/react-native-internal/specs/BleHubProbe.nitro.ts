import type { HybridObject } from 'react-native-nitro-modules'

// Integration projections of crates/blehub/src/contract/errors.rs, not a new BLE contract.
// Nitro uppercases union members in C++; prefix to avoid the NDK ANDROID macro.
// This private bridge projection does not change the canonical Rust Platform enum.
type NitroPlatform = 'platform_windows' | 'platform_android' | 'platform_apple' | 'platform_linux' | 'platform_test'
type DispatchErrorCategory =
  | 'closed'
  | 'invalid_request'
  | 'invalid_state'
  | 'backend_unavailable'
  | 'ffi_contract'
type EventErrorCategory =
  | 'permission'
  | 'adapter_unavailable'
  | 'invalid_state'
  | 'not_found'
  | 'timeout'
  | 'cancelled'
  | 'connection'
  | 'gatt'
  | 'protocol'
  | 'not_supported'
  | 'backend'
  | 'internal'

export interface NitroPlatformErrorDetails {
  platform: NitroPlatform
  code?: string
  name?: string
  message?: string
}

export interface DispatchError {
  category: DispatchErrorCategory
  detail?: string
  platformDetails?: NitroPlatformErrorDetails
}

export interface EventError {
  category: EventErrorCategory
  message: string
  platformDetails?: NitroPlatformErrorDetails
}

// A projection of BleEvent.CommandCompleted only, not the complete event surface.
export interface CompletionEvent {
  commandId: string
  error?: EventError
}

export interface RfGattDescriptor {
  id: string
  uuid: string
}

export interface RfGattCharacteristic {
  id: string
  uuid: string
  readable: boolean
  writableWithResponse: boolean
  writableWithoutResponse: boolean
  notifiable: boolean
  indicatable: boolean
  descriptors: RfGattDescriptor[]
}

export interface RfGattService {
  id: string
  uuid: string
  primary?: boolean
  characteristics: RfGattCharacteristic[]
}

export interface RfScanEvent {
  kind: string
  commandId?: string
  scanSessionId?: string
  peripheralId?: string
  connectionId?: string
  connectionState?: string
  gattSessionId?: string
  characteristicId?: string
  subscriptionId?: string
  gattServices?: RfGattService[]
  value?: ArrayBuffer
  notificationState?: string
  actualNotificationMode?: string
  rssi?: number
  localName?: string
  advertisedServices?: string[]
  adapterState?: string
  scanAuthorization?: string
  error?: EventError
}

export interface TransportSample {
  id: string
  bytes: ArrayBuffer
  error?: EventError
}

export type RfWriteType = 'with_response' | 'without_response'
export type RfNotificationMode = 'platform_default' | 'require_notification' | 'require_indication'

/** Internal codegen/runtime integration probe; this is not a public BLE API. */
export interface BleHubProbe extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /** Canonical decimal u64 string. Undefined means dispatch accepted, NOT BLE success. */
  initialize(commandId: string): DispatchError | undefined

  /** Register before dispatch. Native code must hand off asynchronously, never call JS on the sink stack. */
  setCompletionListener(listener: (event: CompletionEvent) => void): void
  removeCompletionListener(): void

  /** Register before RF dispatch; events are projected from the same native handoff as completion. */
  setRfScanListener(listener: (event: RfScanEvent) => void): void
  removeRfScanListener(): void
  startRfScan(commandId: string, scanSessionId: string, serviceUuid: string): DispatchError | undefined
  stopRfScan(commandId: string, scanSessionId: string): DispatchError | undefined
  connectRfPeripheral(commandId: string, connectionId: string, peripheralId: string): DispatchError | undefined
  disconnectRfPeripheral(commandId: string, connectionId: string): DispatchError | undefined
  readRfRssi(commandId: string, connectionId: string): DispatchError | undefined
  discoverRfGatt(commandId: string, connectionId: string, gattSessionId: string): DispatchError | undefined
  readRfCharacteristic(commandId: string, connectionId: string, gattSessionId: string, characteristicId: string): DispatchError | undefined
  writeRfCharacteristic(commandId: string, connectionId: string, gattSessionId: string, characteristicId: string, value: ArrayBuffer, writeType: RfWriteType): DispatchError | undefined
  setRfNotifications(commandId: string, connectionId: string, gattSessionId: string, subscriptionId: string, characteristicId: string, enabled: boolean, mode: RfNotificationMode): DispatchError | undefined

  /** Transport-only type probe; bytes are not sent over BLE and this is not RF evidence. */
  roundTrip(sample: TransportSample): TransportSample

  /** Resolve only after native Hub disposal; Nitro dispose()/GC is not this contract. */
  close(): Promise<void>
}
