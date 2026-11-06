import { useRef, useState } from 'react'
import type { RefObject } from 'react'
import type {
  BlengScan, BlengScanAdvertisement, BlengScanFailureInfo, BlengScanStop, BlengSessionOwner,
} from '@blehub/react-native-bleng'

export type ScanAction = 'begin-scan' | 'scan-start-wait' | 'scan-pull' | 'scan-stop'
  | 'scan-cancel-start' | 'scan-cancel-stop'
type Phase = 'idle' | 'accepted' | 'starting' | 'running' | 'stopPending' | 'stopped'
  | 'failed' | 'ended' | 'ownerClosed'
interface ScanView {
  phase: Phase
  accepted: boolean
  retained: boolean
  total: number
  samples: string[]
  error?: string
  ended?: string

}
interface ScanRun {
  owner: BlengSessionOwner
  scan: BlengScan
  stop?: BlengScanStop
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
interface Inputs {
  ownerRef: RefObject<BlengSessionOwner | undefined>
  ownerReady: boolean
  controlsEnabled: boolean
  record: (action: ScanAction) => number
  settle: (sequence: number, result: 'succeeded' | 'failed', detail?: string) => void
}
const emptyView = (): ScanView => ({ phase: 'idle', accepted: false, retained: false, total: 0, samples: [] })
const message = (cause: unknown): string => cause instanceof Error ? cause.message : String(cause)
export function scanFailureText(failure: BlengScanFailureInfo): string {
  const platform = failure.platform
  return `${failure.domain}/${failure.code}: ${failure.message}${platform
    ? ` [${platform.platform}/${platform.code ?? 'not reported'}${platform.name === undefined ? '' : ` ${platform.name}`}${platform.message === undefined ? '' : `: ${platform.message}`}]` : ''}`
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

export function useManualScan({ ownerRef, ownerReady, controlsEnabled, record, settle }: Inputs) {
  const runRef = useRef<ScanRun | undefined>(undefined)
  const [view, setView] = useState<ScanView>(emptyView)
  const [timeoutText, setTimeoutText] = useState('5000')
  const timeout = /^\d+$/.test(timeoutText) ? Number(timeoutText) : NaN
  const validTimeout = Number.isSafeInteger(timeout) && timeout > 0
  const current = (run: ScanRun, observation = run.observation): boolean =>
    runRef.current === run && ownerRef.current === run.owner && run.observation === observation
  const publish = (run: ScanRun, patch: Partial<ScanView> = {}): void => {
    if (!current(run)) return
    if (patch.phase) run.phase = patch.phase
    setView(previous => ({ ...previous, ...patch }))
  }
  const usable = (): ScanRun | undefined => {
    const run = runRef.current
    return controlsEnabled && run && current(run) && !run.terminal ? run : undefined
  }
  const reset = (): void => {
    runRef.current = undefined
    setView(emptyView())
  }
  const ownerClosing = (): void => {
    const run = runRef.current
    // Keep all controls until close succeeds, but fence already-pending observations now.
    if (run) run.observation = {}
  }
  const ownerClosed = (): void => {
    const run = runRef.current
    if (run) {
      publish(run, { phase: 'ownerClosed', retained: false })
      runRef.current = undefined
    }
  }
  const canStart = ownerReady && validTimeout && (!runRef.current || runRef.current.terminal)
  const start = async (): Promise<void> => {
    const owner = ownerRef.current
    if (!ownerReady || !validTimeout || !owner || (runRef.current && !runRef.current.terminal)) return
    runRef.current = undefined
    const submission = record('begin-scan')
    let scan: BlengScan
    try {
      const result = owner.beginScan({ serviceUuids: [], allowDuplicates: false })
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
    const run: ScanRun = { owner, scan, observation: {}, phase: 'accepted', terminal: false,
      startPending: true, pullPending: false, stopPending: false, stopWaitConsumed: false,
      cancelStartPending: false, cancelStopPending: false }
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
        run.stop = result.operation
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
  return { view, timeoutText, setTimeoutText, validTimeout, canStart,
    canPull: active && validTimeout && !runRef.current?.pullPending,
    canStop: active && validTimeout && !runRef.current?.stopPending,
    canCancelStart: active && !runRef.current?.cancelStartPending,
    canCancelStop: active && !!runRef.current?.stop && !runRef.current.cancelStopPending,
    stopTitle: !runRef.current?.stop ? 'Stop scan'
      : runRef.current.stopWaitConsumed && !runRef.current.stopPending ? 'Submit another stop' : 'Observe original stop',
    start, pull, stop, cancel, reset, ownerClosing, ownerClosed }
}
