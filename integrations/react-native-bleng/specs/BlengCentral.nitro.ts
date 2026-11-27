import type { HybridObject } from 'react-native-nitro-modules'
import type {
  BlengAdapterObservationSubmission,
} from './BlengAdapterObservation.nitro'
import type { BlengReceipt, BlengFailure, BlengFailureInfo } from './BlengFailure.nitro'
import type { BlengCompanionPresenceSubmission } from './BlengCompanionPresence.nitro'
import type { BlengScanOptions, BlengPeripheral, BlengScanSubmission } from './BlengScan.nitro'

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

/**
 * Owner closure is independent of cleanup success; neither a failure nor a closed owner proves
 * resource-zero. A close can therefore report `closed: true` with a cleanup failure.
 */
export interface BlengCloseResult {
  closed: boolean
  failure?: BlengFailureInfo
}

/** The initialization evidence of one successful owner assembly. */
export interface BlengInitializeOutcome {
  snapshot: BlengAdapterSnapshot
}

/** A transition failure is data, like every other operation outcome. */
export type BlengInitializeResult = BlengInitializeOutcome | BlengFailure

/** Single-consumer adapter-power receipt with independent cancellation. */
export interface BlengAdapterControl extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengAdapterControlAccepted {
  control: BlengAdapterControl
}

export type BlengAdapterControlSubmission = BlengAdapterControlAccepted | BlengFailure

/**
 * One bounded companion-device association receipt.
 *
 * Android shows the platform's own chooser for the request. `wait` resolves when the platform
 * reports an association, fails when the request is declined or unavailable, and is never
 * cancellable after submission. The association itself is data: read it with
 * `beginDeviceQuery('companion', [])` and adopt a key before connecting.
 */
export interface BlengCompanionAssociation
  extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengReceipt>
  cancel(): Promise<BlengReceipt>
}

export interface BlengCompanionAssociationAccepted {
  association: BlengCompanionAssociation
}

export type BlengCompanionAssociationSubmission =
  | BlengCompanionAssociationAccepted
  | BlengFailure

export type BlengDeviceQueryKind = 'connected' | 'bonded' | 'companion'

/** One read-only inventory entry; it cannot address native state. */
export interface BlengDeviceInfo {
  /** Opaque and owner-scoped; comparable with `peripheral.key()` for the same peer. */
  key: string
  localName?: string
  connected: boolean
  bonded: boolean
}

export interface BlengDeviceQueryOutcome {
  devices: BlengDeviceInfo[]
}

/** A read-only inventory snapshot or a typed failure. */
export type BlengDeviceQueryResult = BlengDeviceQueryOutcome | BlengFailure

export interface BlengDeviceQuery extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  wait(timeoutMs: number): Promise<BlengDeviceQueryResult>
  cancel(): Promise<BlengReceipt>
}

export interface BlengDeviceQueryAccepted {
  query: BlengDeviceQuery
}

export type BlengDeviceQuerySubmission = BlengDeviceQueryAccepted | BlengFailure

/** One explicitly adopted inventory key; nothing is connected here. */
export interface BlengPeripheralAdoptAccepted {
  peripheral: BlengPeripheral
}

export type BlengPeripheralAdoptSubmission = BlengPeripheralAdoptAccepted | BlengFailure

/**
 * Apple/Android typed session bridge backed by the Rust Supervisor.
 *
 * Start returns the actual initialization snapshot even when radio is off or access is denied.
 * No raw Bleng object, backend handle, operation ID or recovery context/plan crosses into JS.
 * Scan control/results delegate to Rust; observations carry opaque peripheral capabilities.
 */
export interface BlengCentral extends HybridObject<{ ios: 'swift'; android: 'kotlin' }> {
  /**
   * Assembles the owner and returns the initialization snapshot.
   *
   * Bounded by the library (currently 30 s for owner assembly, including backend attach and the
   * initialize operation). A bound hit is reported as `bleng.timeout`; native work may still be
   * running, so `close()` is the only meaningful next step.
   */
  initialize(): Promise<BlengInitializeResult>
  beginScan(options: BlengScanOptions): BlengScanSubmission
  /**
   * Requests a radio power change where the platform supports it.
   *
   * Success only means the platform accepted the request; the adapter lane reports the actual
   * state. Android may require user confirmation (targetSdk 33+) and Apple has no such API, so it
   * may refuse with `bleng.dispatch_rejected.not_supported`.
   */
  beginAdapterControl(enabled: boolean): BlengAdapterControlSubmission

  /**
   * Requests one companion-device association where the platform supports it (Android API 33+).
   *
   * `deviceProfile` is the host's own platform profile choice, for example
   * `android.app.role.COMPANION_DEVICE_WATCH`. Declaring the matching
   * `REQUEST_COMPANION_PROFILE_*` permission is the host App's responsibility; this package
   * declares no companion permission and no default profile. Success means the platform reported
   * an association, not that a device is present or connected. Apple refuses with
   * `bleng.dispatch_rejected.not_supported`.
   *
   * `notifyOnDeviceNearby` opts this association in to presence delivery. It requires the host to
   * declare `REQUEST_OBSERVE_COMPANION_DEVICE_PRESENCE`; without the declaration the command fails
   * before showing the chooser instead of silently leaving an empty presence lane. Presence itself
   * is read with `beginCompanionPresence()`.
   */
  beginCompanionAssociation(
    serviceUuids: string[],
    displayName?: string,
    deviceProfile?: string,
    notifyOnDeviceNearby?: boolean,
  ): BlengCompanionAssociationSubmission
  /**
   * Reads a read-only system inventory. Results are data (`BlengDeviceInfo`), not connectable
   * capabilities. Android supports all three kinds; Apple supports only `connected`, requires at
   * least one service UUID, and refuses `bonded` and `companion` with
   * `bleng.dispatch_rejected.not_supported`.
   */
  beginDeviceQuery(
    kind: BlengDeviceQueryKind,
    serviceUuids: string[],
  ): BlengDeviceQuerySubmission
  /**
   * Adopts one read-only inventory key as a peripheral capability.
   *
   * Nothing connects here: the returned capability still submits a normal `beginConnect()`, and a
   * backend that did not allocate the key refuses it. This is how an iOS-restored connection is
   * re-addressed after relaunch, without making every inventory record implicitly connectable.
   */
  adoptPeripheral(key: string): BlengPeripheralAdoptSubmission
  /** The adapter's own reported state, as a stream; observation only. */
  beginAdapterObservation(): BlengAdapterObservationSubmission
  /**
   * Live presence transitions for peers associated with `notifyOnDeviceNearby`.
   *
   * Observation only: it cannot associate or connect, and it never changes the adapter. Android
   * reports transitions while the observation is open; Apple has no association model and refuses
   * with `bleng.dispatch_rejected.not_supported`. Open it after `beginCompanionAssociation()` resolves.
   */
  beginCompanionPresence(): BlengCompanionPresenceSubmission
  close(): Promise<BlengCloseResult>
}
