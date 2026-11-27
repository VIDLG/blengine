import { useRef, useState } from 'react'
import type { RefObject } from 'react'
import {
  pendingOperation, BlengOperationError,
} from '@blehub/react-native-bleng'
import type { PendingOperation } from '@blehub/react-native-bleng'
import type {
  BlengReceipt, BlengConnectOutcome, BlengConnection, BlengEndReason,
  BlengConnectionNext,
  BlengMtuInfo,
  BlengReadOutcome,
  BlengDisconnect, BlengGattDiscovery, BlengGattSessionOutcome, BlengNotificationMode,
  BlengNotificationModeRequest, BlengSubscription, BlengPeripheral, BlengRead, BlengScan,
  BlengScanAdvertisement, BlengFailureInfo, BlengScanStop, BlengCentral, BlengWriteType,
  BlengDeviceQueryOutcome,
} from '@blehub/react-native-bleng'
import {
  projectGatt, gattCharacteristicText, gattPropertyFlags, resolveGattTarget,
} from './gattProjection'
import type { GattProjection } from './gattProjection'

export type ScanAction = 'begin-scan' | 'scan-start-wait' | 'scan-pull' | 'scan-stop'
  | 'scan-cancel-start' | 'scan-cancel-stop'
  | 'connect' | 'connect-wait' | 'disconnect' | 'disconnect-wait'
  | 'gatt-discover' | 'gatt-discover-wait' | 'gatt-read' | 'gatt-read-wait'
  | 'gatt-read-cancel' | 'gatt-write' | 'gatt-write-wait' | 'rssi'
  | 'notify-enable' | 'notify-enable-wait' | 'notify-pull' | 'notify-disable'
  | 'notify-disable-wait'
  | 'mtu-query' | 'mtu-query-wait' | 'mtu-request' | 'mtu-request-wait'
  | 'priority' | 'priority-wait' | 'refresh' | 'refresh-wait'
  | 'descriptor-read' | 'descriptor-read-wait'
  | 'descriptor-write' | 'descriptor-write-wait'
  | 'devices-query' | 'devices-query-wait'
  | 'auto-connect' | 'auto-connect-wait'
  | 'adapter-control' | 'adapter-control-wait'

type Phase = 'idle' | 'accepted' | 'starting' | 'running' | 'stopPending' | 'stopped'
  | 'failed' | 'ended' | 'ownerClosed'
type Link = 'none' | 'connecting' | 'connected' | 'disconnecting' | 'disconnected'
/**
 * What ended the link, once something did.
 *
 * `none` means no end has been observed. `requested` is a disconnect this App submitted (the
 * package's own word for "the caller asked"), `unrequested` is an end nobody here asked for, and
 * `ownerClosed` is the engine being retired, which ends the observation rather than proving the
 * link is down. The workbench adds no attribution vocabulary of its own.
 */
type LinkEnd = 'none' | BlengEndReason
type GattPhase = 'none' | 'discovering' | 'ready' | 'failed'
/**
 * The read lane's own phase, because a read can be cancelled by the operator.
 *
 * `cancelled` is a terminal the operator asked for, not a peer fault, so it must be
 * distinguishable from `failed` — a scene asserts the difference.
 */
type GattRead = 'none' | 'waiting' | 'cancelled' | 'done' | 'failed'
/**
 * The subscription's own phase.
 *
 * `unverified` is a refused *disable*: the command failed, so whether values still arrive
 * is not something the App may claim either way. A platform may keep delivering (the peer
 * is still enabled) or stop locally when it refuses the write; claiming `subscribed` here
 * would be a silent divergence from what the operator sees. A later value returns the
 * phase to `subscribed`, and the stream's own terminal ends it.
 */
type NotifyPhase = 'none' | 'enabling' | 'subscribed' | 'unverified' | 'ended' | 'failed'
/**
 * The terminal one link-fact operation reached.
 *
 * `unsupported` is a *platform answer*, not a failure: CoreBluetooth cannot request an MTU,
 * set a connection priority or invalidate its attribute cache, and Android has no bonded
 * inventory on iOS. Keeping it apart from `failed` is what lets a scene assert "the App
 * reported the platform's own refusal" instead of either hoping for success or accepting a
 * timeout as an outcome.
 */
type LinkFact = 'none' | 'pending' | 'done' | 'unsupported' | 'failed'
/**
 * The terminal of the one descriptor this workbench addresses: the CCCD.
 *
 * `absent` is a fact, not a failure: a characteristic without a CCCD cannot carry a
 * subscription, and the App says so instead of reporting a read that never happened.
 */
type DescriptorPhase = 'none' | 'done' | 'absent' | 'failed'
/** The system inventories the platform can answer for; `connected` is the main line's. */
type DeviceKind = 'connected' | 'bonded' | 'companion'
/** Which direction a radio request asked the platform to go. */
type RadioTarget = 'off' | 'on'
interface ScanView {
  phase: Phase
  link: Link
  linkEnd: LinkEnd
  gattPhase: GattPhase
  gatt?: GattProjection
  gattError?: string
  gattResult?: string
  gattRead: GattRead
  gattCancel?: string
  rssi?: number
  rssiError?: string
  notifyPhase: NotifyPhase
  notifyMode?: BlengNotificationMode
  notifyError?: string
  notifyTotal: number
  notifySamples: string[]
  /** Link facts the platform may legitimately not support; each states its own terminal. */
  mtuPhase: LinkFact
  /** `<withResponse>/<withoutResponse>` from a completed MTU query, if the platform reported any. */
  mtuValue?: string
  mtuRequestPhase: LinkFact
  priorityPhase: LinkFact
  refreshPhase: LinkFact
  /** The resolved target characteristic's CCCD, as the peer reports it. */
  descriptorPhase: DescriptorPhase
  /** The CCCD's current bytes, lowercase hex without separators, when it was read. */
  descriptorValue?: string
  /**
   * The one descriptor write this workbench makes: it sends back exactly the bytes the
   * preceding read reported.
   *
   * `unsupported` is a platform answer, not a failure: CoreBluetooth refuses a direct write to
   * a Client Characteristic Configuration descriptor (it demands `setNotifyValue(_:for:)`),
   * so the App reports that refusal as itself rather than as a failed write.
   */
  descriptorWritePhase: 'none' | 'done' | 'unsupported' | 'failed'
  /**
   * The system inventory query across a live link: `none` before it, `pending` while its
   * wait is outstanding, then `done`, `unsupported` (a platform that has no such inventory)
   * or `failed`. The count and the peer match are separate facts because "a list came back"
   * and "the list contains the link I am holding" are different claims.
   */
  deviceQueryPhase: LinkFact
  /** Entries the completed `connected` inventory reported, or absent before one. */
  deviceCount?: number
  /** Whether that inventory contained the peer this run is linked to. */
  devicePeer?: boolean
  /** Which inventory the completed query asked for: `connected`, `bonded` or `companion`. */
  deviceKind?: DeviceKind
  /**
   * The standing "connect when the peer appears" lane.
   *
   * `unsupported` is the platform saying it has no such capability (CoreBluetooth), not a
   * failure; `done` means the link exists and `link` carries it.
   */
  autoPhase: LinkFact
  /**
   * The radio-control lane: `done` only when the platform actually accepted the change.
   *
   * `unsupported` is a platform answer, not a failure. It is the *expected* answer on this
   * project's devices: Android's own `BluetoothAdapter` note says an app targeting API 33+
   * cannot enable or disable the adapter (the call always returns false) unless it is a
   * device/profile owner or a system app, and CoreBluetooth never exposes radio control.
   */
  radioPhase: LinkFact
  /** Which direction the last radio request asked for: `off`, `on`, or nothing yet. */
  radioTarget?: RadioTarget
  accepted: boolean
  retained: boolean
  total: number
  samples: string[]
  error?: string
  ended?: string

}
interface ScanRun {
  central: BlengCentral
  scan: BlengScan
  stop?: BlengScanStop
  // Latest opaque observation capability; never a device ID and never a retained payload.
  peripheral?: BlengPeripheral
  connection?: BlengConnection
  gatt?: GattProjection
  /**
   * Reads whose wait is still outstanding, oldest first.
   *
   * The native lane is a bounded FIFO, so a second read submitted while the first is
   * waiting is *queued*, not rejected. Keeping each wait's own capability and intent is
   * what lets the operator cancel the queued one while the active one still completes.
   */
  reads: OutstandingRead[]
  /**
   * How many read waits this run has settled, any terminal.
   *
   * `reads` is the live queue (0 -> 1 -> 0), so it says "something is outstanding" but cannot
   * tell two sequential reads apart. A scene that submits a second read needs the monotonic
   * count: the App keeps showing the previous read's value, so only a settled-operation
   * count distinguishes "the read I just asked for finished" from "the last one did".
   */
  readTotal: number
  /**
   * How many descriptor reads this run has settled, any terminal.
   *
   * Same reason as `readTotal`: the App keeps the previous CCCD value on screen, so a scene
   * whose expected bytes equal that value cannot tell a fresh read from a carried-over one
   * without a monotonic count. A plan advances this field to force a read it cannot infer.
   */
  descriptorReadTotal: number
  cancelReadPending: boolean
  notifications?: BlengSubscription
  notifyPending: boolean
  notifyTerminal: boolean
  /** A refused disable: the phase says `unverified` until a value proves otherwise. */
  unverifiedNotification: boolean
  connectPending: boolean
  disconnectPending: boolean
  gattPending: boolean
  /** One link-fact operation at a time: the backend serializes link I/O anyway. */
  linkFactPending: boolean
  descriptorPending: boolean
  /** One inventory query at a time; it is an owner command, not a link command. */
  deviceQueryPending: boolean
  /** One standing auto-connect request at a time. */
  autoPending: boolean
  /**
   * Set by the first observed end of this link, and never cleared while the run exists.
   *
   * Two facts can report one end — the operator's own disconnect receipt and the connection's
   * retained end — and whichever arrives second must not resurrect the link.
   */
  linkEnded: boolean
  // Local observation identity only; never a native generation or device ID.
  observation: object
  phase: Phase
  terminal: boolean
  startPending: boolean
  pullPending: boolean
  stopPending: boolean
  stopWaitConsumed: boolean
  cancelStartPending: boolean
  cancelStopPending: boolean
}
interface OutstandingRead {
  /** The one pending wait, kept because cancelling is a separate, explicit control. */
  pending: PendingOperation<BlengReadOutcome>
  /** Set by this run's own cancel control, never inferred from a native code alone. */
  cancelRequested: boolean
}
interface Inputs {
  centralRef: RefObject<BlengCentral | undefined>
  centralReady: boolean
  controlsEnabled: boolean
  record: (action: ScanAction) => number
  settle: (sequence: number, result: 'succeeded' | 'failed', detail?: string) => void
}
const emptyView = (): ScanView => ({
  phase: 'idle', link: 'none', linkEnd: 'none', gattPhase: 'none', notifyPhase: 'none',
  accepted: false, retained: false, total: 0, samples: [], notifyTotal: 0, notifySamples: [],
  gattRead: 'none',
  mtuPhase: 'none', mtuRequestPhase: 'none', priorityPhase: 'none', refreshPhase: 'none',
  descriptorPhase: 'none', descriptorWritePhase: 'none',
  deviceQueryPhase: 'none',
  autoPhase: 'none',
  radioPhase: 'none',
})
const message = (cause: unknown): string => cause instanceof Error ? cause.message : String(cause)
/**
 * One failure's ledger text, whether it arrived as a native record or through the await layer.
 *
 * `pendingOperation` raises the same `BlengFailureInfo` a submission would have returned, so the
 * two paths cannot drift into different wording in the App's own record.
 */
const operationFailureText = (cause: unknown): string =>
  cause instanceof BlengOperationError ? scanFailureText(cause.failure) : message(cause)
/**
 * BLeng's own cancellation code, as the supervisor projects it.
 *
 * `crates/bleng-supervisor/src/ffi/failure.rs` composes `bleng.<code>`, so the code for
 * `BlengError::Cancelled` is `bleng.cancelled` — not `cancelled`. Comparing the bare word
 * meant a cancellation this run had requested was reported as a peer fault.
 */
const CANCELLED_CODE = 'bleng.cancelled'
/**
 * The projection's identity for "your own deadline expired".
 *
 * A deadline is the caller's bound, not a fact about the link, so an end observer re-arms after
 * one instead of treating it as a result.
 */
/**
 * The one code a platform uses for "this capability does not exist on this OS".
 *
 * `crates/bleng-supervisor/src/ffi/failure.rs` composes the code as `<path>.not_supported`:
 * `bleng.dispatch_rejected.not_supported` when the engine refuses a submission, and
 * `bleng.operation.not_supported` when the backend reports the refusal as the operation's own
 * terminal (Android's adapter power request takes that path). One answer, two shapes — so the
 * check is on the terminal segment. Matching one full string reported a correct platform
 * refusal as a failure (measured on `adapter-control`, 2026-11-26).
 */
const NOT_SUPPORTED_SUFFIX = '.not_supported'
/**
 * Whether a failure is the platform saying "this capability does not exist here".
 *
 * `unsupported` is an answer and `failed` is a fault; they must never be conflated, and neither
 * may depend on which boundary the refusal came from.
 */
const isNotSupported = (failure: BlengFailureInfo | undefined): boolean =>
  failure !== undefined
  && (failure.code === 'not_supported' || failure.code.endsWith(NOT_SUPPORTED_SUFFIX))
/**
 * The MTU this App asks for when the operator requests one.
 *
 * 247 leaves 244 bytes of ATT payload, the largest single write that fits the default
 * 4-byte L2CAP header without fragmentation on the platforms we test. It is a *request*:
 * the peer may negotiate less, and CoreBluetooth refuses the request entirely.
 */
const EXPECTED_MTU = 247
const OBSERVATION_TIMEOUT_CODE = 'bleng.timeout'
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
/**
 * Parse the manual scan filter: comma-separated 128-bit UUIDs, or nothing.
 *
 * The scan lane keeps a bounded advertisement budget, so an unfiltered scan in a dense
 * environment fails closed. Filtering is the API's own answer and the request must
 * therefore state exactly which UUIDs it wants, never a half-parsed list.
 */
export function parseScanFilter(text: string): { uuids: string[]; error?: string } {
  const items = text.split(',').map(item => item.trim().toLowerCase()).filter(Boolean)
  for (const item of items) {
    if (!CANONICAL_UUID.test(item)) return { uuids: [], error: `Not a full 128-bit UUID: ${item.slice(0, 40)}` }
  }
  return { uuids: [...new Set(items)] }
}
export function scanFailureText(failure: BlengFailureInfo): string {
  const platform = failure.platform
  return `${failure.domain}/${failure.code}: ${failure.message}${platform
    ? ` [${platform.platform}${platform.code === undefined ? '' : `/${platform.code}`}${platform.name === undefined ? '' : ` ${platform.name}`}${platform.message === undefined ? '' : `: ${platform.message}`}]` : ''}`
}
function binary(payload: ArrayBuffer): string {
  const prefix = Array.from(new Uint8Array(payload, 0, Math.min(16, payload.byteLength)),
    byte => byte.toString(16).padStart(2, '0')).join(' ')
  return `${payload.byteLength} bytes [${prefix}${payload.byteLength > 16 ? ' …' : ''}]`
}
function limited(values: string[], limit: number): string {
  return `${values.slice(0, limit).map(value => value.slice(0, 160)).join(', ') || 'none'}${values.length > limit ? ` (+${values.length - limit} omitted)` : ''}`
}
function summary(value: BlengScanAdvertisement): string {
  // Do not retain the opaque peripheral, payload buffers, or unbounded native arrays in UI state.
  const manufacturers = value.manufacturerData.slice(0, 4).map(item => `company=${item.companyId}: ${binary(item.payload)}`)
  const services = value.serviceData.slice(0, 4).map(item => `service=${item.serviceUuid.slice(0, 160)}: ${binary(item.payload)}`)
  return `name=${value.localName === undefined ? 'not reported' : JSON.stringify(value.localName.slice(0, 160))}; rssi=${value.rssi ?? 'not reported'}; connectable=${value.connectable ?? 'not reported'}\nservices=${limited(value.advertisedServices, 8)}\nmanufacturer=${manufacturers.join(', ') || 'none'}${value.manufacturerData.length > 4 ? ' (more omitted)' : ''}\nservice data=${services.join(', ') || 'none'}${value.serviceData.length > 4 ? ' (more omitted)' : ''}`
}

export function useManualScan({ centralRef, centralReady, controlsEnabled, record, settle }: Inputs) {
  const runRef = useRef<ScanRun | undefined>(undefined)
  /**
   * The owner-scoped inventory probe.
   *
   * `bonded` and `companion` are facts about the adapter, not steps of a link, so the Probe page
   * must be able to answer them before any scan exists — a scene may ask them on a session that
   * never started one. They remain bounded to one owner: the request carries the `BlengCentral`
   * it was made through, and an answer is published only while that exact request is still the
   * live one. `connected` stays inside the run, because its whole claim is a comparison against
   * that run's link.
   */
  const inventoryRef = useRef<
    { central: BlengCentral; observation: object; pending: boolean } | undefined
  >(undefined)
  const inventoryPendingFor = (central: BlengCentral | undefined): boolean => {
    const probe = inventoryRef.current
    return central !== undefined && probe !== undefined && probe.central === central && probe.pending
  }
  /**
   * The owner-scoped radio-control record.
   *
   * The radio is an adapter fact, not a step of a link: it must be answerable before any scan
   * exists and it must survive a run that ended, so it is fenced on the owner exactly like the
   * inventory probe (see `workbench-page-architecture.md` R6). The answer is published only
   * while this exact request is still the live one, and `ownerClosing` retires it.
   */
  const radioRef = useRef<
    { central: BlengCentral; observation: object; pending: boolean } | undefined
  >(undefined)
  const radioPendingFor = (central: BlengCentral | undefined): boolean => {
    const held = radioRef.current
    return central !== undefined && held !== undefined && held.central === central && held.pending
  }
  const [view, setView] = useState<ScanView>(emptyView)
  const [timeoutText, setTimeoutText] = useState('5000')
  const timeout = /^\d+$/.test(timeoutText) ? Number(timeoutText) : NaN
  const validTimeout = Number.isSafeInteger(timeout) && timeout > 0
  const [filterText, setFilterText] = useState('')
  const filter = parseScanFilter(filterText)
  const validFilter = filter.error === undefined
  const current = (run: ScanRun, observation = run.observation): boolean =>
    runRef.current === run && centralRef.current === run.central && run.observation === observation
  const publish = (run: ScanRun, patch: Partial<ScanView> = {}): void => {
    if (!current(run)) return
    const next = { ...patch }
    // A link that ended stays ended. `publish` is the single writer of `link`, so the guard belongs
    // here rather than at each of the paths that can report an end.
    if (run.linkEnded && next.link !== undefined && next.link !== 'disconnected') delete next.link
    if (next.phase) run.phase = next.phase
    setView(previous => ({ ...previous, ...next }))
  }
  const usable = (): ScanRun | undefined => {
    const run = runRef.current
    return controlsEnabled && run && current(run) && !run.terminal ? run : undefined
  }
  const reset = (): void => {
    runRef.current = undefined
    inventoryRef.current = undefined
    radioRef.current = undefined
    setView(emptyView())
  }
  /**
   * Records the link's one end, whichever fact reported it first.
   *
   * Only a live link can offer the commands retired here; the topology stays on screen as history,
   * but it is no longer addressable. A reported platform failure for the end is kept as the run's
   * newest failure; an end without one never clears a failure that really happened.
   */
  const settleEnd = (run: ScanRun, observation: object, by: BlengEndReason,
    failure?: BlengFailureInfo): void => {
    if (!current(run, observation) || run.linkEnded) return
    run.linkEnded = true
    // A subscription cannot outlive the link that carries it. Retiring the phase here is what
    // keeps the App from claiming `subscribed` on a link it is reporting dead, and the lane's
    // terminal is what stops offering a pull that could only be answered over that link.
    const carriedSubscription = run.notifications !== undefined && !run.notifyTerminal
    run.connection = undefined
    run.gatt = undefined
    run.notifications = undefined
    if (carriedSubscription) run.notifyTerminal = true
    const detail = failure && scanFailureText(failure)
    publish(run, {
      link: 'disconnected', linkEnd: by,
      ...(carriedSubscription
        ? { notifyPhase: 'ended' as const, notifyError: undefined }
        : {}),
      ...(detail === undefined ? {} : { error: detail }),
    })
  }
  const ownerClosing = (): void => {
    const run = runRef.current
    // Keep all controls until close succeeds, but fence already-pending observations now.
    if (run) {
      run.observation = {}
      // A closing engine must not lend its observation or connection capabilities to new commands.
      run.peripheral = undefined
      run.connection = undefined
      run.notifications = undefined
    }
    // The inventory probe is the other capability this owner lent out; retiring the record is
    // what stops an in-flight answer from being published by a session that is already closing.
    inventoryRef.current = undefined
    radioRef.current = undefined
  }
  const ownerClosed = (): void => {
    const run = runRef.current
    if (run) {
      publish(run, { phase: 'ownerClosed', retained: false })
      runRef.current = undefined
    }
  }
  const canStart = centralReady && validTimeout && validFilter
    && (!runRef.current || runRef.current.terminal)
  const start = async (): Promise<void> => {
    const central = centralRef.current
    if (!centralReady || !validTimeout || !validFilter || !central || (runRef.current && !runRef.current.terminal)) return
    runRef.current = undefined
    const submission = record('begin-scan')
    let scan: BlengScan
    try {
      // The filter is the scan request itself, so the run never silently scans wider.
      const result = central.beginScan({ serviceUuids: filter.uuids, allowDuplicates: false })
      if ('failure' in result) {
        const detail = scanFailureText(result.failure)
        settle(submission, 'failed', detail)
        setView({ ...emptyView(), phase: 'failed', error: detail })
        return
      }
      scan = result.scan
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = message(cause)
      settle(submission, 'failed', detail)
      setView({ ...emptyView(), phase: 'failed', error: detail })
      return
    }
    const run: ScanRun = { central, scan, observation: {}, phase: 'accepted', terminal: false,
      startPending: true, pullPending: false, stopPending: false, stopWaitConsumed: false,
      cancelStartPending: false, cancelStopPending: false, connectPending: false,
      disconnectPending: false, linkEnded: false, gattPending: false, notifyPending: false,
      linkFactPending: false, descriptorPending: false,
      deviceQueryPending: false,
      autoPending: false,
      reads: [], readTotal: 0, descriptorReadTotal: 0, cancelReadPending: false,
      notifyTerminal: false, unverifiedNotification: false }
    runRef.current = run
    setView({ ...emptyView(), phase: 'accepted', accepted: true, retained: true })
    const observation = run.observation
    const sequence = record('scan-start-wait')
    publish(run, { phase: 'starting' })
    try {
      const result = await scan.waitUntilStarted(timeout)
      const detail = result.failure && scanFailureText(result.failure)
      // Historical evidence settles even when this observation can no longer update the scan UI.
      settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) publish(run, {
        ...(!run.stop && run.phase !== 'stopPending' ? { phase: detail === undefined ? 'running' : 'failed' } : {}),
        error: detail,
      })
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) publish(run, { error: detail, ...(!run.stop ? { phase: 'failed' } : {}) })
    } finally {
      run.startPending = false
      if (current(run, observation)) publish(run)
    }
  }
  const pull = async (): Promise<void> => {
    const run = usable()
    if (!run || run.pullPending || !validTimeout) return
    run.pullPending = true
    const observation = run.observation
    const sequence = record('scan-pull')
    publish(run)
    try {
      const result = await run.scan.nextEvent(timeout)
      if ('failure' in result) {
        const detail = scanFailureText(result.failure)
        settle(sequence, 'failed', detail)
        if (!current(run, observation)) return
        if (!run.terminal) publish(run, { error: detail, phase: run.stopPending ? 'stopPending' : 'failed' })
      } else if ('ended' in result) {
        const end = result.ended
        const detail = end.failure ? scanFailureText(end.failure) : end.reason === 'failed' ? 'Scan ended: failed (no failure details reported)' : undefined
        settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail ?? `ended: ${end.reason}`)
        if (!current(run, observation)) return
        run.terminal = true
        publish(run, { phase: end.reason === 'ownerClosed' ? 'ownerClosed' : detail ? 'failed'
          : end.reason === 'requested' ? 'stopped' : 'ended', ended: end.reason, error: detail, retained: false })
      } else {
        settle(sequence, 'succeeded')
        if (!current(run, observation)) return
        if (!run.terminal) {
          // Keep only the newest capability; earlier observations are never re-addressed.
          run.peripheral = result.advertisement.peripheral
          const sample = summary(result.advertisement)
          setView(previous => ({ ...previous, total: previous.total + 1, samples: [...previous.samples, sample].slice(-8) }))
        }
      }
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) publish(run, { error: detail, phase: run.stopPending ? 'stopPending' : 'failed' })
    } finally {
      run.pullPending = false
      if (current(run, observation)) publish(run)
    }
  }
  const stop = async (): Promise<void> => {
    const run = usable()
    if (!run || run.stopPending || !validTimeout) return
    const observation = run.observation
    const sequence = record('scan-stop')
    run.stopPending = true
    publish(run, { phase: 'stopPending' })
    try {
      if (!run.stop || run.stopWaitConsumed) {
        const result = run.scan.beginStop()
        if ('failure' in result) {
          const detail = scanFailureText(result.failure)
          settle(sequence, 'failed', detail)
          publish(run, { phase: 'failed', error: detail })
          return
        }
        run.stop = result.stop
      }
      // A wait consumes its result even on timeout/failure; retain the handle for cancel.
      run.stopWaitConsumed = true
      const result = await run.stop.wait(timeout)
      if (result.failure?.domain === 'input' && result.failure.code === 'invalid_timeout') {
        run.stopWaitConsumed = false
      }
      const detail = result.failure && scanFailureText(result.failure)
      settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) {
        if (detail === undefined) run.terminal = true
        publish(run, { phase: detail === undefined ? 'stopped' : 'failed', error: detail, retained: detail !== undefined })
      }
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) publish(run, { phase: 'failed', error: detail })
    } finally {
      run.stopPending = false
      if (current(run, observation)) publish(run)
    }
  }
  const cancel = async (target: 'start' | 'stop'): Promise<void> => {
    const run = usable()
    const key = target === 'start' ? 'cancelStartPending' : 'cancelStopPending'
    if (!run || run[key] || (target === 'stop' && !run.stop)) return
    run[key] = true
    const observation = run.observation
    const sequence = record(target === 'start' ? 'scan-cancel-start' : 'scan-cancel-stop')
    publish(run)
    try {
      const result = await (target === 'start' ? run.scan.cancelStart() : run.stop!.cancel())
      const detail = result.failure && scanFailureText(result.failure)
      settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail)
      if (!current(run, observation)) return
      if (detail && !run.terminal) publish(run, { error: detail })
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      if (!run.terminal) publish(run, { error: detail })
    } finally {
      run[key] = false
      if (current(run, observation)) publish(run)
    }
  }
  const active = !!usable()
  // A capability outlives the scan session that observed it, so link controls do not require a
  // non-terminal scan. They still require the same live owner and a non-retired observation.
  const linkable = (): ScanRun | undefined => {
    const run = runRef.current
    if (!controlsEnabled || !run || !current(run) || !run.peripheral) return undefined
    return run
  }
  /** A command that needs a live link, as the App itself reports the link. */
  const linked = (): boolean => view.link === 'connected' && !!linkable()
  /**
   * Watches the end of the link this run established, on that connection's own lane.
   *
   * This is the only way to learn about an end nobody here caused: the engine reports the terminal
   * on the capability that owns the link, so this sends no command, reads no device state and never
   * infers an end from another command's failure. A deadline is not a fact, so it re-arms; anything
   * else the lane reports is a failure of observation itself, and watching stops.
   */
  const observeLinkEnd = async (run: ScanRun, observation: object, connection: BlengConnection):
  Promise<void> => {
    for (;;) {
      let next: BlengConnectionNext
      try {
        next = await connection.awaitEnd(timeout)
      } catch {
        return
      }
      if (!current(run, observation)) return
      if ('ended' in next) {
        settleEnd(run, observation, next.ended.reason, next.ended.failure)
        return
      }
      if (next.failure.code !== OBSERVATION_TIMEOUT_CODE) return
    }
  }
  const connect = async (): Promise<void> => {
    const run = linkable()
    const peripheral = run?.peripheral
    if (!run || !peripheral || run.connectPending || !validTimeout) return
    run.connectPending = true
    const observation = run.observation
    const submission = record('connect')
    publish(run, { link: 'connecting', error: undefined })
    // One accepted operation, awaited through the shared layer: a refused submission throws
    // here (before any wait is submitted) and a settled failure arrives through the same
    // handle, so the App cannot forget the second check.
    let pending: PendingOperation<BlengConnectOutcome>
    try {
      pending = pendingOperation(peripheral.beginConnect(), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { link: 'none', error: detail })
      return
    } finally {
      run.connectPending = false
    }
    const sequence = record('connect-wait')
    try {
      const result = await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      if (!result.connection) {
        // The type says a connection is always here. A native answer that is not one must not
        // become a claimed link merely because the layer typed it as accepted.
        publish(run, { link: 'none', error: 'native bridge returned no connection' })
        return
      }
      run.connection = result.connection
      publish(run, { link: 'connected', error: undefined })
      // The lane exists before this call returns; a link that already ended is still retained.
      void observeLinkEnd(run, observation, result.connection)
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { link: 'none', error: detail })
    }
  }
  /**
   * Ask the platform to link whenever the peer becomes available.
   *
   * This is not `connect` with a longer wait: the scene proves the difference by asking while
   * the peer is *absent*, so a link that only appears after the peer returns cannot be the
   * plain connect path. Apple has no equivalent capability and refuses with `not_supported`;
   * that answer is published as `unsupported`, never as a failure or a timeout.
   */
  const autoConnect = async (): Promise<void> => {
    const run = linkable()
    const peripheral = run?.peripheral
    if (!run || !peripheral || run.autoPending || view.link === 'connected' || !validTimeout) return
    run.autoPending = true
    const observation = run.observation
    const submission = record('auto-connect')
    publish(run, { autoPhase: 'pending', link: 'connecting', error: undefined })
    const terminal = (detail: string, failure?: BlengFailureInfo): Partial<ScanView> => ({
      autoPhase: isNotSupported(failure) ? 'unsupported' : 'failed',
      link: 'none',
      error: detail,
    })
    let pending: PendingOperation<BlengConnectOutcome>
    try {
      pending = pendingOperation(peripheral.beginAutoConnect(), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      run.autoPending = false
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
      return
    } finally {
      run.autoPending = false
    }
    const sequence = record('auto-connect-wait')
    try {
      const result = await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      if (!result.connection) {
        publish(run, { autoPhase: 'failed', link: 'none', error: 'native bridge returned no connection' })
        return
      }
      run.connection = result.connection
      publish(run, { autoPhase: 'done', link: 'connected', error: undefined })
      void observeLinkEnd(run, observation, result.connection)
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
    }
  }
  const disconnect = async (): Promise<void> => {
    const run = linkable()
    const connection = run?.connection
    if (!run || !connection || run.disconnectPending || !validTimeout) return
    run.disconnectPending = true
    const observation = run.observation
    const submission = record('disconnect')
    publish(run, { link: 'disconnecting', error: undefined })
    let pending: PendingOperation<unknown>
    try {
      pending = pendingOperation(connection.beginDisconnect(), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { link: 'connected', error: detail })
      return
    } finally {
      run.disconnectPending = false
    }
    const sequence = record('disconnect-wait')
    try {
      await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      // A successful disconnect is the same fact the connection's own lane retains, so it goes
      // through the one writer of link ends rather than publishing a second, divergent view.
      settleEnd(run, observation, 'requested')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      // A failed disconnect keeps the link: native state is unknown, not disconnected.
      publish(run, { link: 'connected', error: detail })
    }
  }
  /** Discovery needs a live connection capability, not merely a previous topology on screen. */
  const discoverable = (): ScanRun | undefined => {
    const run = runRef.current
    if (!controlsEnabled || !run || !current(run) || !run.connection) return undefined
    return run
  }
  const discover = async (): Promise<void> => {
    const run = discoverable()
    const connection = run?.connection
    if (!run || !connection || run.gattPending || !validTimeout) return
    run.gattPending = true
    const observation = run.observation
    const submission = record('gatt-discover')
    publish(run, { gattPhase: 'discovering', gattError: undefined, gattResult: undefined })
    let pending: PendingOperation<BlengGattSessionOutcome>
    try {
      pending = pendingOperation(connection.beginDiscoverGatt(), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { gattPhase: 'failed', gattError: detail })
      return
    } finally {
      run.gattPending = false
    }
    const sequence = record('gatt-discover-wait')
    try {
      const result = await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      if (!result.session) {
        // Same rule as the connect lane: a typed success that is not actually a session must
        // not become a claimed topology.
        publish(run, { gattPhase: 'failed', gattError: 'native bridge returned no session' })
        return
      }
      // The session capability is consumed once here; the UI keeps only a bounded projection.
      run.gatt = projectGatt(result.session)
      publish(run, { gattPhase: 'ready', gatt: run.gatt, gattError: undefined })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { gattPhase: 'failed', gattError: detail })
    }
  }
  const read = async (target: string): Promise<void> => {
    const run = linkable()
    const entry = resolveGattTarget(run?.gatt, target).entry
    if (!run || !entry || run.gattPending || !validTimeout) return
    run.gattPending = true
    const observation = run.observation
    const submission = record('gatt-read')
    publish(run, { gattError: undefined, gattResult: undefined, gattCancel: undefined, gattRead: 'none' })
    let outstanding: OutstandingRead | undefined
    let pending: PendingOperation<BlengReadOutcome>
    try {
      pending = pendingOperation(entry.capability.beginRead(), timeout)
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      run.gattPending = false
      if (!current(run, observation)) return
      publish(run, { gattError: detail, gattRead: 'failed' })
      return
    }
    run.gattPending = false
    // The submission settles here: `beginRead` returned a capability, and the wait that
    // follows is a separate receipt. Leaving this pending would hide an accepted read.
    settle(submission, 'succeeded')
    // The wait keeps its own handle and intent, so cancelling is a separate channel and
    // never has to be inferred from a native code after the fact.
    outstanding = { pending, cancelRequested: false }
    run.reads.push(outstanding)
    publish(run, { gattRead: 'waiting' })
    const sequence = record('gatt-read-wait')
    try {
      const value = await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      if (value.value === undefined) {
        publish(run, { gattError: 'native bridge returned no value', gattRead: 'failed' })
        return
      }
      publish(run, { gattResult: `${entry.uuid}: ${binary(value.value)}`, gattRead: 'done' })
    } catch (cause) {
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      const detail = failure ? scanFailureText(failure) : message(cause)
      const cancelled =
        failure?.domain === 'operation' && failure.code === CANCELLED_CODE
        && outstanding.cancelRequested
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      if (cancelled) {
        // A requested cancellation is a terminal the operator asked for, not a peer fault.
        publish(run, { gattRead: 'cancelled', gattCancel: detail, gattError: undefined })
      } else {
        publish(run, { gattError: detail, gattRead: 'failed' })
      }
    } finally {
      run.gattPending = false
      run.reads = run.reads.filter(item => item !== outstanding)
      // Count the wait, not the submission: a read that never produced a wait has no
      // terminal to report, and its failure is already `gattRead=failed`.
      if (outstanding !== undefined) run.readTotal += 1
      if (current(run, observation)) publish(run)
    }
  }
  /**
   * Cancel the newest outstanding read; the older ones keep waiting.
   *
   * BLeng's cancel is independent of the result and is not rollback. The cancelled wait
   * still settles its own receipt (it did not succeed), and this run publishes
   * `gattRead=cancelled` only for a cancellation it requested itself. A rejected cancel
   * leaves the wait outstanding and names the rejection, so a request is never read as a
   * result.
   */
  const cancelRead = async (): Promise<void> => {
    const run = linkable()
    const target = run?.reads[run.reads.length - 1]
    if (!run || !target || run.cancelReadPending) return
    run.cancelReadPending = true
    target.cancelRequested = true
    const observation = run.observation
    const sequence = record('gatt-read-cancel')
    publish(run)
    try {
      const result = await target.pending.cancel()
      const detail = result.failure && scanFailureText(result.failure)
      settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail)
      if (!current(run, observation)) return
      if (detail !== undefined) publish(run, { gattError: detail, gattRead: 'waiting' })
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { gattError: detail })
    } finally {
      run.cancelReadPending = false
      if (current(run, observation)) publish(run)
    }
  }
  const write = async (target: string, text: string): Promise<void> => {
    const run = linkable()
    const entry = resolveGattTarget(run?.gatt, target).entry
    if (!run || !entry || run.gattPending || !validTimeout) return
    run.gattPending = true
    const observation = run.observation
    const submission = record('gatt-write')
    publish(run, { gattError: undefined, gattResult: undefined })
    const writeType: BlengWriteType =
      entry.properties.writableWithResponse ? 'withResponse' : 'withoutResponse'
    const payload = new TextEncoder().encode(text)
    let pending: PendingOperation<unknown>
    try {
      pending = pendingOperation(
        entry.capability.beginWrite(payload.buffer, writeType), timeout,
      )
      run.gattPending = false
      // The submission settles here: `beginWrite` returned a capability, and the wait
      // that follows is a separate receipt. Leaving this pending would hide an accepted
      // write behind the same `pending` label a never-answered submission would show.
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { gattError: detail })
      return
    } finally {
      run.gattPending = false
    }
    const sequence = record('gatt-write-wait')
    try {
      await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      // A completion receipt is not a read-back; the UI never claims the peer stored the value.
      publish(run, {
        gattResult: `${entry.uuid}: wrote ${payload.byteLength} bytes (${writeType})`,
      })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { gattError: detail })
    }
  }
  const readRssi = async (): Promise<void> => {
    const run = discoverable()
    const connection = run?.connection
    if (!run || !connection || run.gattPending || !validTimeout) return
    run.gattPending = true
    const observation = run.observation
    const submission = record('rssi')
    publish(run, { rssi: undefined, rssiError: undefined })
    let pending: PendingOperation<{ rssi: number }>
    try {
      pending = pendingOperation(connection.beginReadRssi(), timeout)
      run.gattPending = false
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { rssiError: detail })
      return
    } finally {
      run.gattPending = false
    }
    try {
      const reading = await pending.result
      settle(submission, 'succeeded')
      if (!current(run, observation)) return
      if (reading.rssi === undefined) {
        publish(run, { rssiError: 'native bridge returned no rssi' })
        return
      }
      publish(run, { rssi: reading.rssi, rssiError: undefined })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { rssiError: detail })
    }
  }
  /**
   * One link-fact operation (MTU query/request, connection priority, cache refresh).
   *
   * The four lanes share everything that must not drift: the submission is awaited through
   * the same layer, the ledger records submission and wait separately, and the terminal is
   * one of `done` / `unsupported` / `failed`. `unsupported` is read from the platform's own
   * refusal code, because "this OS cannot do it" is an answer a scene must be able to assert
   * without accepting a timeout or a failure in its place.
   */
  const linkFact = async (
    action: 'mtu-query' | 'mtu-request' | 'priority' | 'refresh',
    field: 'mtuPhase' | 'mtuRequestPhase' | 'priorityPhase' | 'refreshPhase',
    submit: (connection: BlengConnection) => object,
    applied: (receipt: object) => Partial<ScanView>,
  ): Promise<void> => {
    const run = linkable()
    const connection = run?.connection
    if (!run || !connection || run.linkFactPending || !validTimeout) return
    run.linkFactPending = true
    const observation = run.observation
    const submission = record(action)
    publish(run, { [field]: 'pending' } as Partial<ScanView>)
    const terminal = (detail: string, failure?: BlengFailureInfo): Partial<ScanView> => ({
      [field]: isNotSupported(failure) ? 'unsupported' : 'failed',
      error: detail,
    } as Partial<ScanView>)
    let pending: PendingOperation<Record<string, unknown>>
    try {
      pending = pendingOperation(submit(connection), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
      return
    } finally {
      run.linkFactPending = false
    }
    const sequence = record(`${action}-wait` as ScanAction)
    try {
      const receipt = await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      publish(run, { ...applied(receipt), [field]: 'done' } as Partial<ScanView>)
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
    }
  }
  const queryMtu = (): Promise<void> => linkFact(
    'mtu-query',
    'mtuPhase',
    connection => connection.beginQueryMtu(),
    receipt => {
      const info = (receipt as { info?: BlengMtuInfo }).info
      const withResponse = info?.maximumWriteWithResponse
      const withoutResponse = info?.maximumWriteWithoutResponse
      // The negotiated MTU is absent on CoreBluetooth, so `mtuValue` carries what both
      // platforms do report: the two write limits. `none` is a platform that reports neither.
      if (withResponse === undefined && withoutResponse === undefined) return {}
      return {
        mtuValue: `${withResponse ?? 'none'}/${withoutResponse ?? 'none'}`,
      }
    },
  )
  const requestMtu = (): Promise<void> => linkFact(
    'mtu-request',
    'mtuRequestPhase',
    connection => connection.beginRequestMtu(EXPECTED_MTU),
    () => ({}),
  )
  const requestHighPriority = (): Promise<void> => linkFact(
    'priority',
    'priorityPhase',
    connection => connection.beginSetConnectionPriority('high'),
    () => ({}),
  )
  const refreshGattCache = (): Promise<void> => linkFact(
    'refresh',
    'refreshPhase',
    connection => connection.beginGattCacheRefresh(),
    () => ({}),
  )
  /**
   * Read the platform's own `connected` inventory and check it against the live link.
   *
   * Two separate facts are published: how many entries came back, and whether the peer this
   * run is actually linked to appears among them. A list that merely exists proves the command
   * was accepted; only the peer match proves the inventory describes the link the App holds.
   * The key is owner-scoped and documented as comparable with `peripheral.key()`, so the App
   * never compares addresses or names.
   */
  /**
   * Read one platform inventory and publish what it answered.
   *
   * `connected` is the observation main line's question: does the list contain the peer this
   * run holds. `bonded` and `companion` are capability probes — a platform may legitimately
   * have no such inventory, and that refusal is published as `unsupported`, never as a failure.
   * The never-compared facts are stated separately: how many entries came back, whether this
   * run's peer was among them, and which inventory was asked for.
   */
  const queryDevices = async (kind: DeviceKind = 'connected'): Promise<void> => {
    const run = runRef.current
    const central = centralRef.current
    // A `connected` inventory is only meaningful next to a link; the other kinds are facts
    // about the adapter, so they need the owner but not a connection.
    const peripheral = run?.peripheral
    if (!central || !validTimeout || !controlsEnabled) return
    if (run?.deviceQueryPending || inventoryPendingFor(central)) return
    if (kind === 'connected' && (!run || !peripheral || view.link !== 'connected')) return
    // The live run, when there is one, is this request's fence and its target: `publish` already
    // refuses to update a run whose observation retired. Without a run the fence is the owner
    // record itself, which is what a `bonded`/`companion` probe is a fact about.
    const scope = run !== undefined && current(run) ? run : undefined
    const probe = { central, observation: {}, pending: true }
    const release = (): void => {
      if (scope === undefined) probe.pending = false
      else scope.deviceQueryPending = false
    }
    const publishQuery = (patch: Partial<ScanView>): void => {
      if (scope !== undefined) {
        publish(scope, patch)
        return
      }
      // Publishing without a run is legal only for the owner that was actually asked, and only
      // while its record is still the live one.
      if (centralRef.current !== central || inventoryRef.current !== probe) return
      setView(previous => ({ ...previous, ...patch }))
    }
    if (scope === undefined) inventoryRef.current = probe
    else scope.deviceQueryPending = true
    const submission = record('devices-query')
    publishQuery({
      deviceQueryPhase: 'pending',
      deviceKind: kind,
      deviceCount: undefined,
      devicePeer: undefined,
      error: undefined,
    })
    const terminal = (detail: string, failure?: BlengFailureInfo): Partial<ScanView> => ({
      deviceQueryPhase: isNotSupported(failure) ? 'unsupported' : 'failed',
      error: detail,
    })
    let pending: PendingOperation<BlengDeviceQueryOutcome>
    try {
      // The declared service filter is the inventory request itself: Apple requires at least
      // one UUID, and Android reads its own system inventory. An unusable filter is still sent
      // as an empty list rather than a guess the operator never declared.
      pending = pendingOperation(
        central.beginDeviceQuery(kind, filter.error === undefined ? filter.uuids : []),
        timeout,
      )
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      release()
      publishQuery(terminal(detail, failure))
      return
    }
    const sequence = record('devices-query-wait')
    try {
      const receipt = await pending.result
      settle(sequence, 'succeeded')
      // Only the `connected` inventory has a peer to compare against; `bonded` and
      // `companion` are adapter facts and publish no peer match at all (`none`).
      const key = peripheral?.key()
      release()
      publishQuery({
        deviceQueryPhase: 'done',
        deviceCount: receipt.devices.length,
        devicePeer: key === undefined
          ? undefined
          : receipt.devices.some(device => device.key === key),
      })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      release()
      publishQuery(terminal(detail, failure))
    }
  }
  /**
   * Ask the platform to change the radio's power state.
   *
   * This is an adapter fact, not a link fact: it needs the owner and nothing else, and it is
   * fenced on the owner record (R6) so a session that never scanned can still ask.
   *
   * `unsupported` is an answer, not a failure — and on the devices this project tests it is the
   * *expected* answer: Android refuses adapter power changes for any app targeting API 33+ that
   * is not a device/profile owner or a system app (`BluetoothAdapter.enable/disable` then always
   * return false, per the platform's own note this repo keeps at
   * `docs/platform-api-reference/android/BluetoothAdapter.java`), and CoreBluetooth exposes no
   * radio control at all. `done` therefore means the platform really accepted the request; the
   * App never reports `done` on its own say-so.
   */
  const setRadio = async (enabled: boolean): Promise<void> => {
    const central = centralRef.current
    const target: RadioTarget = enabled ? 'on' : 'off'
    if (!central || !validTimeout || !controlsEnabled) return
    const held = radioRef.current
    if (held !== undefined && held.central === central && held.pending) return
    const request = { central, observation: {}, pending: true }
    radioRef.current = request
    const publishRadio = (patch: Partial<ScanView>): void => {
      // Publishing is legal only for the owner that was asked, and only while its record is
      // still the live one.
      if (centralRef.current !== central || radioRef.current !== request) return
      setView(previous => ({ ...previous, ...patch }))
    }
    const terminal = (detail: string, failure?: BlengFailureInfo): Partial<ScanView> => ({
      radioPhase: isNotSupported(failure) ? 'unsupported' : 'failed',
      error: detail,
    })
    const submission = record('adapter-control')
    publishRadio({ radioPhase: 'pending', radioTarget: target, error: undefined })
    let pending: PendingOperation<BlengReceipt>
    try {
      pending = pendingOperation(central.beginAdapterControl(enabled), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      request.pending = false
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publishRadio(terminal(detail, failure))
      return
    }
    const sequence = record('adapter-control-wait')
    try {
      await pending.result
      settle(sequence, 'succeeded')
      request.pending = false
      // The receipt is the platform accepting the request, not the radio's new state: the only
      // surface that reports a state change is the adapter observation lane (L-tier
      // `adapter-transition`), and this App never claims one from a receipt.
      publishRadio({ radioPhase: 'done' })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      request.pending = false
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publishRadio(terminal(detail, failure))
    }
  }
  /**
   * The CCCD of the resolved characteristic, or undefined when it has none.
   *
   * The CCCD is addressed by its Bluetooth SIG UUID rather than by a caller-declared one:
   * it is the one descriptor whose value the App itself is responsible for, and the only one
   * a subscription claim can be checked against.
   */
  const descriptorCapability = () => {
    const entry = resolveGattTarget(runRef.current?.gatt, gattTarget).entry
    return entry?.descriptorCapabilities.find(
      descriptor => descriptor.uuid().toLowerCase().startsWith('00002902'),
    )
  }
  /**
   * Read the peer's own CCCD value.
   *
   * This is a second witness for the subscription phase: the App says `subscribed`, and the
   * descriptor the peer stores says whether its CCC notifications are enabled. Reading it is
   * a separate command with its own terminal, never an inference from the notification lane.
   */
  const readDescriptor = async (): Promise<void> => {
    const run = linkable()
    const descriptor = descriptorCapability()
    if (!run || !validTimeout || run.descriptorPending) return
    run.descriptorPending = true
    const observation = run.observation
    const submission = record('descriptor-read')
    if (descriptor === undefined) {
      // A characteristic without a CCCD cannot be subscribed to. That is a fact about the
      // peer's topology, so it is published as itself rather than as a failed read.
      run.descriptorPending = false
      publish(run, { descriptorPhase: 'absent', descriptorValue: undefined })
      return
    }
    let pending: PendingOperation<{ value?: ArrayBuffer }>
    try {
      pending = pendingOperation(descriptor.beginRead(), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      run.descriptorPending = false
      if (!current(run, observation)) return
      publish(run, { descriptorPhase: 'failed', error: detail })
      return
    } finally {
      run.descriptorPending = false
    }
    const sequence = record('descriptor-read-wait')
    try {
      const receipt = await pending.result
      run.descriptorReadTotal += 1
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      const bytes = receipt.value
      if (bytes === undefined) {
        publish(run, { descriptorPhase: 'failed', error: 'native bridge returned no descriptor value' })
        return
      }
      publish(run, {
        descriptorPhase: 'done',
        descriptorValue: Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join(''),
      })
    } catch (cause) {
      run.descriptorReadTotal += 1
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { descriptorPhase: 'failed', error: detail })
    }
  }
  /**
   * Write the bytes the peer already reported back to its CCCD.
   *
   * Writing the same value is the one descriptor write a workbench can make without changing
   * subscription state: a different value would desync the peer from the lane that owns it.
   * The write is therefore only offered after a read, and it proves the write path without
   * claiming an effect the App cannot verify.
   */
  const writeDescriptor = async (): Promise<void> => {
    const run = linkable()
    const descriptor = descriptorCapability()
    const value = view.descriptorValue
    if (!run || !validTimeout || run.descriptorPending) return
    if (descriptor === undefined || value === undefined) return
    run.descriptorPending = true
    const observation = run.observation
    const submission = record('descriptor-write')
    // Same vocabulary as every other platform-refusable command: the platform's own
    // `not_supported` is an answer, and a bare failure stays a failure.
    const terminal = (detail: string, failure?: BlengFailureInfo): Partial<ScanView> => ({
      descriptorWritePhase: isNotSupported(failure) ? 'unsupported' : 'failed',
      error: detail,
    })
    const bytes = new Uint8Array(value.match(/.{2}/g)?.map(pair => Number.parseInt(pair, 16)) ?? [])
    let pending: PendingOperation<BlengReceipt>
    try {
      pending = pendingOperation(descriptor.beginWrite(bytes.buffer), timeout)
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      run.descriptorPending = false
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
      return
    } finally {
      run.descriptorPending = false
    }
    const sequence = record('descriptor-write-wait')
    try {
      await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      publish(run, { descriptorWritePhase: 'done' })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      const failure = cause instanceof BlengOperationError ? cause.failure : undefined
      publish(run, terminal(detail, failure))
    }
  }
  /** A subscription is a stream: it stays readable after the run's scan is stopped. */
  const subscribable = (): ScanRun | undefined => {
    const run = runRef.current
    if (!controlsEnabled || !run || !current(run) || !run.gatt) return undefined
    return run
  }
  const enableNotifications = async (target: string): Promise<void> => {
    const run = subscribable()
    const entry = resolveGattTarget(run?.gatt, target).entry
    if (!run || !entry || run.notifyPending || !validTimeout) return
    if (!entry.properties.notifiable && !entry.properties.indicatable) return
    // Ask for exactly what the characteristic advertises, so the platform never has to guess.
    const mode: BlengNotificationModeRequest = entry.properties.notifiable
      ? 'requireNotification'
      : 'requireIndication'
    run.notifyPending = true
    const observation = run.observation
    const submission = record('notify-enable')
    publish(run, { notifyPhase: 'enabling', notifyError: undefined })
    try {
      const result = entry.capability.beginSubscription(mode)
      if ('failure' in result) {
        const detail = scanFailureText(result.failure)
        settle(submission, 'failed', detail)
        if (!current(run, observation)) return
        publish(run, { notifyPhase: 'failed', notifyError: detail })
        return
      }
      run.notifications = result.notifications
      settle(submission, 'succeeded')
    } catch (cause) {
      const detail = message(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { notifyPhase: 'failed', notifyError: detail })
      return
    } finally {
      run.notifyPending = false
    }
    const notifications = run.notifications
    // A lane was admitted but the subscription was never enabled. Observation ends here
    // explicitly instead of waiting for owner retirement: nothing was enabled, so there is
    // nothing to disable, and a retry must be judged by the peer's own answer rather than by
    // this attempt's leftover bookkeeping. `detach` ends observation only and claims no disable.
    // (The engine admits several subscription lanes side by side, so this is hygiene, not a
    // requirement for the retry to be accepted.)
    const abandonLane = (): void => {
      try {
        notifications.detach()
        run.notifications = undefined
      } catch {
        // A detach failure cannot turn a refused enable into a subscription, and it must
        // not overwrite the peer's refusal as this wait's detail: the lane then ends with
        // the owner, which is where an observation-only lane ends anyway.
      }
    }
    const sequence = record('notify-enable-wait')
    try {
      const receipt = await notifications.waitUntilEnabled(timeout)
      const failure = 'failure' in receipt ? receipt.failure : undefined
      const detail = failure && scanFailureText(failure)
      settle(sequence, detail === undefined ? 'succeeded' : 'failed', detail)
      if (!current(run, observation)) return
      if (detail !== undefined) {
        publish(run, { notifyPhase: 'failed', notifyError: detail })
        abandonLane()
      } else {
        publish(run, {
          notifyPhase: 'subscribed',
          // An unreported mode stays absent rather than being guessed from the request.
          notifyMode: 'mode' in receipt ? receipt.mode : undefined,
          notifyError: undefined,
        })
      }
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { notifyPhase: 'failed', notifyError: detail })
      abandonLane()
    }
  }
  const pullNotification = async (): Promise<void> => {
    const run = linkable()
    const notifications = run?.notifications
    if (!run || !notifications || run.notifyPending || run.notifyTerminal || !validTimeout) return
    run.notifyPending = true
    const observation = run.observation
    const sequence = record('notify-pull')
    try {
      const result = await notifications.nextEvent(timeout)
      if ('failure' in result) {
        const detail = scanFailureText(result.failure)
        settle(sequence, 'failed', detail)
        if (!current(run, observation)) return
        // An observation timeout is not a dead subscription, so it must not end the phase.
        publish(run, { notifyError: detail })
      } else if ('ended' in result) {
        settle(sequence, 'succeeded', `ended: ${result.ended.reason}`)
        if (!current(run, observation)) return
        run.notifyTerminal = true
        publish(run, { notifyPhase: 'ended', notifyError: undefined })
      } else {
        settle(sequence, 'succeeded')
        if (!current(run, observation)) return
        const sample = binary(result.value)
        setView(previous => ({
          ...previous,
          notifyTotal: previous.notifyTotal + 1,
          notifySamples: [...previous.notifySamples, sample].slice(-8),
        }))
        // A delivered value is proof that the subscription is live: it resolves the
        // uncertainty a refused disable left, and it retires that refusal's detail.
        if (run.unverifiedNotification) {
          run.unverifiedNotification = false
          publish(run, { notifyPhase: 'subscribed', notifyError: undefined })
        }
      }
    } catch (cause) {
      const detail = message(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { notifyError: detail })
    } finally {
      run.notifyPending = false
      if (current(run, observation)) publish(run)
    }
  }
  const disableNotifications = async (): Promise<void> => {
    const run = linkable()
    const notifications = run?.notifications
    if (!run || !notifications || run.notifyPending || !validTimeout) return
    run.notifyPending = true
    const observation = run.observation
    const submission = record('notify-disable')
    let pending: PendingOperation<unknown>
    try {
      pending = pendingOperation(notifications.beginDisable(), timeout)
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(submission, 'failed', detail)
      if (!current(run, observation)) return
      publish(run, { notifyError: detail })
      return
    }
    settle(submission, 'succeeded')
    run.notifyPending = false
    const sequence = record('notify-disable-wait')
    try {
      await pending.result
      settle(sequence, 'succeeded')
      if (!current(run, observation)) return
      // Disable is a receipt *and* the stream's terminal: once the peer acknowledged the
      // CCCD write there is no further value to wait for, so this publishes `ended` here
      // instead of leaving the phase `subscribed` until some later pull happens to observe
      // it. A second pull is then never required — and never offered, because the lane is
      // closed on both sides.
      run.notifyTerminal = true
      run.unverifiedNotification = false
      publish(run, { notifyPhase: 'ended', notifyError: undefined })
    } catch (cause) {
      const detail = operationFailureText(cause)
      settle(sequence, 'failed', detail)
      if (!current(run, observation)) return
      run.unverifiedNotification = true
      publish(run, { notifyPhase: 'unverified', notifyError: detail })
    } finally {
      run.notifyPending = false
    }
  }
  // The characteristic the action strip acts on, declared by UUID. The App resolves it
  // against its own bounded projection; nothing outside the App needs to know a row key.
  const [gattTarget, setGattTarget] = useState('')
  const resolution = resolveGattTarget(runRef.current?.gatt, gattTarget)
  const resolved = resolution.entry
  return { view, timeoutText, setTimeoutText, validTimeout, canStart,
    filterText, setFilterText, filterError: filter.error,
    filterSummary: filter.uuids.length === 0
      ? 'Filter: none (unfiltered scan)'
      : `Filter: ${filter.uuids.join(', ')}`,
    gattTarget, setGattTarget,
    gattTargetStatus: resolution.status,
    gattTargetMatches: resolution.matches,
    gattTargetProperties: resolved === undefined ? 'none' : gattPropertyFlags(resolved),
    canPull: active && validTimeout && !runRef.current?.pullPending,
    canStop: active && validTimeout && !runRef.current?.stopPending,
    canCancelStart: active && !runRef.current?.cancelStartPending,
    canCancelStop: active && !!runRef.current?.stop && !runRef.current.cancelStopPending,
    canConnect: !!linkable() && validTimeout && !!runRef.current?.peripheral
      && !runRef.current.connectPending && view.link !== 'connected',
    canDisconnect: !!linkable() && validTimeout && !!runRef.current?.connection
      && !runRef.current.disconnectPending && view.link === 'connected',
    // Everything that needs a live link is gated on the link the App itself reports. A
    // projection that says `disconnected` (or `connecting`) must not keep offering
    // commands that can only fail — including after a peer-initiated end, where the App
    // learns about it from the failing I/O and then from its own disconnect.
    canDiscoverGatt: linked() && validTimeout && !runRef.current?.gattPending,
    canReadGatt: linked() && validTimeout && !!resolved?.properties.readable
      && !runRef.current?.gattPending,
    // Read from the live queue, not a copy: the view is a render snapshot, and the
    // queue is the fact the cancel control and its projection both describe.
    gattReads: runRef.current?.reads.length ?? 0,
    gattReadTotal: runRef.current?.readTotal ?? 0,
    canCancelGattRead: !!linkable() && view.gattRead === 'waiting'
      && !runRef.current?.cancelReadPending,
    canWriteGatt: linked() && validTimeout
      && !!(resolved?.properties.writableWithResponse
        || resolved?.properties.writableWithoutResponse)
      && !runRef.current?.gattPending,
    canReadRssi: linked() && validTimeout && !runRef.current?.gattPending,
    // One link fact at a time, and only over a live link: these four answer about the link
    // itself, so they share one gate rather than four near-identical ones.
    canLinkFact: linked() && validTimeout && !runRef.current?.linkFactPending,
    canReadDescriptor: linked() && validTimeout && !!resolved && !runRef.current?.descriptorPending,
    canWriteDescriptor: linked() && validTimeout && !!resolved && !runRef.current?.descriptorPending
      && view.descriptorValue !== undefined,
    descriptorReadTotal: runRef.current?.descriptorReadTotal ?? 0,
    canQueryDevices: linked() && validTimeout && !runRef.current?.deviceQueryPending,
    // Inventory probes other than `connected` are adapter facts: they need the owner, not a link.
    canQueryInventory: controlsEnabled && validTimeout
      && !inventoryPendingFor(centralRef.current) && !runRef.current?.deviceQueryPending,
    // The standing request is offered only while there is no link: asking for "connect when
    // it appears" on a live link would make the scene unable to tell the two paths apart.
    canAutoConnect: !!linkable() && validTimeout && view.link !== 'connected'
      && !runRef.current?.autoPending,
    // Radio control is an owner fact: it needs the owner, never a link (R6).
    canSetRadio: controlsEnabled && validTimeout && !radioPendingFor(centralRef.current),
    canEnableNotifications: linked() && validTimeout
      && !!(resolved?.properties.notifiable || resolved?.properties.indicatable)
      && !runRef.current?.notifyPending,
    canPullNotification: linked() && validTimeout && !!runRef.current?.notifications
      && !runRef.current.notifyPending && !runRef.current.notifyTerminal,
    canDisableNotifications: !!subscribable() && validTimeout && !runRef.current?.notifyPending
      && !!runRef.current?.notifications,
    gattCharacteristicText,
    stopTitle: !runRef.current?.stop ? 'Stop scan'
      : runRef.current.stopWaitConsumed && !runRef.current.stopPending ? 'Submit another stop' : 'Observe original stop',
    start, pull, stop, cancel, connect, disconnect, discover, read, write, readRssi,
    cancelRead,
    queryMtu, requestMtu, requestHighPriority, refreshGattCache,
    queryDevices, autoConnect, setRadio,
    readDescriptor, writeDescriptor,
    enableNotifications, pullNotification, disableNotifications,
    reset, ownerClosing, ownerClosed }
}
