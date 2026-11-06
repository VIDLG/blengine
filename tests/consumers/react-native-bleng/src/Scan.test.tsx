import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengSessionOwner } from '@blehub/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengPeripheral, BlengScan, BlengScanAdvertisement,
  BlengScanCommandResult, BlengScanFailureInfo, BlengScanNext, BlengScanStop,
  BlengSessionOwner,
} from '@blehub/react-native-bleng'
import App from './App'

jest.mock('@blehub/react-native-bleng', () => ({ createBlengSessionOwner: jest.fn() }))
const createOwner = jest.mocked(createBlengSessionOwner)
afterEach(() => { closeTrace.mockRestore() })
const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized',
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: Error) => void
  const promise = new Promise<T>((finish, fail) => { resolve = finish; reject = fail })
  return { promise, resolve, reject }
}
const timeout: BlengScanFailureInfo = {
  domain: 'observation', code: 'Timeout', message: 'observation deadline',
}
const rejected: BlengScanFailureInfo = {
  domain: 'operation', code: 'Rejected', message: 'native refused',
  platform: { platform: 'apple', code: '15', name: 'ATT', message: 'not permitted' },
}
const invalidTimeout: BlengScanFailureInfo = {
  domain: 'input', code: 'invalid_timeout', message: 'timeout rejected by Rust',
}
const stopTimeout: BlengScanFailureInfo = {
  domain: 'operation', code: 'bleng.timeout', message: 'stop result deadline',
}
function stopFixture() {
  let consumed = false
  const result = jest.fn<Promise<BlengScanCommandResult>, [number]>().mockResolvedValue({})
  return {
    result,
    wait: jest.fn(async (timeoutMs: number): Promise<BlengScanCommandResult> => {
      if (consumed) return { failure: {
        domain: 'operation', code: 'bleng.operation_result_consumed', message: 'result already consumed',
      } }
      // Model Rust's single-consumer slot, including timeout/failure and pending waits.
      consumed = true
      const outcome = await result(timeoutMs)
      if (outcome.failure?.domain === 'input' && outcome.failure.code === 'invalid_timeout') consumed = false
      return outcome
    }),
    cancel: jest.fn<Promise<BlengScanCommandResult>, []>().mockResolvedValue({}),
  }
}
function fixture() {
  const stop = stopFixture()
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengScanCommandResult>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengScanCommandResult>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({ failure: timeout }),
    beginStop: jest.fn().mockReturnValue({ operation: stop as unknown as BlengScanStop }),
  }
  const owner = {
    start: jest.fn().mockResolvedValue(snapshot),
    close: jest.fn().mockResolvedValue({ ownerClosed: true }),
    beginScan: jest.fn().mockReturnValue({ scan: scan as unknown as BlengScan }),
  }
  return { owner, scan, stop }
}
let current: ReturnType<typeof fixture>
let closeTrace: jest.SpyInstance
beforeEach(() => {
  jest.clearAllMocks()
  closeTrace = jest.spyOn(console, 'info').mockImplementation(() => {})
  current = fixture()
  createOwner.mockReturnValue(current.owner as unknown as BlengSessionOwner)
})
async function press(id: string) {
  await act(async () => { fireEvent.press(screen.getByTestId(id)) })
}
async function startedApp() {
  render(<App />)
  await press('start-session')
}
function advertisement(index = 0): BlengScanAdvertisement {
  return {
    peripheral: {} as BlengPeripheral, localName: `sample-${index}`,
    advertisedServices: ['180d'], manufacturerData: [], serviceData: [],
  }
}

it('requires a successfully started owner; accepted scan is not running and uses the configured original wait', async () => {
  const ownerStart = deferred<BlengAdapterSnapshot>()
  current.owner.start.mockReturnValue(ownerStart.promise)
  const scanStart = deferred<BlengScanCommandResult>()
  current.scan.waitUntilStarted.mockReturnValue(scanStart.promise)
  render(<App />)
  await press('workbench-current')
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  expect(createOwner).not.toHaveBeenCalled()
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  await act(async () => { ownerStart.resolve(snapshot) })
  expect(current.owner.beginScan).not.toHaveBeenCalled()
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '1234')
  await press('start-scan')
  expect(current.owner.beginScan).toHaveBeenCalledWith({ serviceUuids: [], allowDuplicates: false })
  expect(current.scan.waitUntilStarted).toHaveBeenCalledWith(1234)
  expect(screen.getByTestId('scan-accepted')).toHaveTextContent(/accepted/)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^starting$/)
  expect(current.scan.nextEvent).not.toHaveBeenCalled()
  fireEvent.press(screen.getByTestId('start-scan'))
  expect(current.owner.beginScan).toHaveBeenCalledTimes(1)
  await press('workbench-tests')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/begin-scan: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-start-wait: pending/)
  await act(async () => { scanStart.resolve({}) })
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-start-wait: succeeded/)
  expect(current.scan.cancelStart).not.toHaveBeenCalled()
  expect(current.scan.beginStop).not.toHaveBeenCalled()
  await press('workbench-current')
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^running$/)
})

it('projects optional advertisement facts and bounded binary prefixes without using a peripheral ID', async () => {
  current.scan.nextEvent.mockResolvedValueOnce({ advertisement: {
    ...advertisement(), localName: '', rssi: 0, connectable: false,
    manufacturerData: [{ companyId: 76, payload: Uint8Array.from({ length: 24 }, (_, i) => i).buffer }],
    serviceData: [{ serviceUuid: '180d', payload: new Uint8Array([255, 0]).buffer }],
  } }).mockResolvedValueOnce({ advertisement: { ...advertisement(), localName: undefined } })
  await startedApp()
  await press('start-scan')
  await press('next-scan-event')
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/name=""/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/rssi=0; connectable=false/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/services=180d/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/company=76: 24 bytes \[00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f …\]/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/service=180d: 2 bytes \[ff 00\]/)
  await press('next-scan-event')
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/name=not reported/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/rssi=not reported; connectable=not reported/)
})

it('displays structured submission and start failures as failures, never successful starts', async () => {
  current.owner.beginScan.mockReturnValueOnce({ failure: rejected })
  current.scan.waitUntilStarted.mockResolvedValue({ failure: rejected })
  await startedApp()
  await press('start-scan')
  expect(current.scan.waitUntilStarted).not.toHaveBeenCalled()
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/operation\/Rejected/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/apple\/15/)
  await press('start-scan')
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  expect(screen.getByTestId('stop-scan')).toBeEnabled()
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-start-wait: failed — operation\/Rejected/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/scan-start-wait: succeeded/)
})

it('keeps the scan after observation timeouts for explicit pull and stop, with no retries', async () => {
  current.scan.waitUntilStarted.mockResolvedValue({ failure: timeout })
  await startedApp()
  await press('start-scan')
  expect(current.scan.waitUntilStarted).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/observation\/Timeout/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  await press('next-scan-event')
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(1)
  current.scan.nextEvent.mockResolvedValue({ advertisement: advertisement() })
  await press('next-scan-event')
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('scan-state')).not.toHaveTextContent(/^running$/)
  await press('stop-scan')
  expect(current.scan.beginStop).toHaveBeenCalledTimes(1)
  expect(current.stop.wait).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopped$/)
})

it.each([stopTimeout, rejected])('requires an explicit new stop after consumed $code, retaining cancel and scan controls', async failure => {
  const replacement = stopFixture()
  current.stop.result.mockResolvedValue({ failure })
  current.scan.beginStop.mockReturnValueOnce({ operation: current.stop as unknown as BlengScanStop })
    .mockReturnValueOnce({ operation: replacement as unknown as BlengScanStop })
  await startedApp()
  await press('start-scan')
  await press('stop-scan')
  expect(current.stop.wait).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  expect(screen.getByTestId('next-scan-event')).toBeEnabled()
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  for (const page of ['tests', 'evidence', 'current']) await press(`workbench-${page}`)
  expect(screen.getByText('Submit another stop')).toBeTruthy()
  expect(replacement.wait).not.toHaveBeenCalled()
  await press('cancel-scan-stop')
  expect(current.stop.cancel).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  await press('stop-scan')
  expect(current.scan.beginStop).toHaveBeenCalledTimes(2)
  expect(current.stop.wait).toHaveBeenCalledTimes(1)
  expect(replacement.wait).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopped$/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence').props.children).toContain(`scan-stop: failed — ${failure.domain}/${failure.code}`)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-stop: succeeded/)
})

it.each(['typed rejection', 'bridge throw'] as const)('keeps the previous cancel handle when another stop submission returns %s', async kind => {
  const replacement = stopFixture()
  current.stop.result.mockResolvedValue({ failure: rejected })
  current.scan.beginStop.mockReturnValueOnce({ failure: rejected })
    .mockReturnValueOnce({ operation: current.stop as unknown as BlengScanStop })
    .mockImplementationOnce(() => {
      if (kind === 'bridge throw') throw new Error('stop submission bridge rejected')
      return { failure: rejected }
    })
    .mockReturnValueOnce({ operation: replacement as unknown as BlengScanStop })
  await startedApp()
  await press('start-scan')
  await press('stop-scan')
  expect(current.stop.wait).not.toHaveBeenCalled()
  expect(screen.getByText('Stop scan')).toBeTruthy()
  expect(screen.getByTestId('cancel-scan-stop')).toBeDisabled()
  await press('stop-scan')
  await press('stop-scan')
  expect(current.scan.beginStop).toHaveBeenCalledTimes(3)
  expect(current.stop.wait).toHaveBeenCalledTimes(1)
  expect(replacement.wait).not.toHaveBeenCalled()
  expect(screen.getByText('Submit another stop')).toBeTruthy()
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  await press('cancel-scan-stop')
  expect(current.stop.cancel).toHaveBeenCalledTimes(1)
  expect(replacement.cancel).not.toHaveBeenCalled()
  await press('stop-scan')
  expect(current.scan.beginStop).toHaveBeenCalledTimes(4)
  expect(current.stop.wait).toHaveBeenCalledTimes(1)
  expect(replacement.wait).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopped$/)
})

it('reuses the unconsumed stop after Rust input/invalid_timeout and a user timeout correction', async () => {
  current.stop.result.mockResolvedValueOnce({ failure: invalidTimeout }).mockResolvedValueOnce({})
  await startedApp()
  await press('start-scan')
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '2147483648')
  await press('stop-scan')
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/input\/invalid_timeout/)
  expect(screen.getByText('Observe original stop')).toBeTruthy()
  expect(screen.getByTestId('cancel-scan-stop')).toBeEnabled()
  for (const page of ['tests', 'evidence', 'current']) await press(`workbench-${page}`)
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '1234')
  await press('stop-scan')
  expect(current.scan.beginStop).toHaveBeenCalledTimes(1)
  expect(current.stop.wait.mock.calls).toEqual([[2147483648], [1234]])
  expect(current.stop.result.mock.calls).toEqual([[2147483648], [1234]])
  expect(current.stop.cancel).not.toHaveBeenCalled()
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopped$/)
})

it('keeps a newly submitted stop wait pending across navigation and independent cancellation without resubmitting', async () => {
  const completion = deferred<BlengScanCommandResult>()
  const cancellation = deferred<BlengScanCommandResult>()
  const replacement = stopFixture()
  current.stop.result.mockResolvedValue({ failure: stopTimeout })
  replacement.result.mockReturnValue(completion.promise)
  replacement.cancel.mockReturnValue(cancellation.promise)
  current.scan.beginStop.mockReturnValueOnce({ operation: current.stop as unknown as BlengScanStop })
    .mockReturnValueOnce({ operation: replacement as unknown as BlengScanStop })
  await startedApp()
  await press('start-scan')
  await press('stop-scan')
  await press('stop-scan')
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopPending$/)
  expect(screen.getByTestId('stop-scan')).toBeDisabled()
  fireEvent.press(screen.getByTestId('stop-scan'))
  for (const page of ['tests', 'evidence', 'current']) await press(`workbench-${page}`)
  expect(screen.getByTestId('stop-scan')).toBeDisabled()
  expect(screen.getByTestId('cancel-scan-stop')).toBeEnabled()
  await press('cancel-scan-stop')
  expect(screen.getByTestId('cancel-scan-stop')).toBeDisabled()
  expect(current.stop.cancel).not.toHaveBeenCalled()
  expect(replacement.cancel).toHaveBeenCalledTimes(1)
  await act(async () => { cancellation.resolve({}) })
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopPending$/)
  expect(screen.getByTestId('stop-scan')).toBeDisabled()
  expect(replacement.wait).toHaveBeenCalledTimes(1)
  await act(async () => { completion.resolve({ failure: rejected }) })
  expect(screen.getByText('Submit another stop')).toBeTruthy()
  expect(screen.getByTestId('stop-scan')).toBeEnabled()
  expect(screen.getByTestId('cancel-scan-stop')).toBeEnabled()
  expect(current.scan.beginStop).toHaveBeenCalledTimes(2)
  expect(current.stop.wait).toHaveBeenCalledTimes(1)
  expect(replacement.wait).toHaveBeenCalledTimes(1)
})

it('allows only one pending pull even through navigation, and never polls automatically', async () => {
  const pull = deferred<BlengScanNext>()
  current.scan.nextEvent.mockReturnValue(pull.promise)
  await startedApp()
  await press('start-scan')
  await press('next-scan-event')
  expect(screen.getByTestId('next-scan-event')).toBeDisabled()
  fireEvent.press(screen.getByTestId('next-scan-event'))
  for (const page of ['tests', 'evidence', 'current']) await press(`workbench-${page}`)
  expect(screen.getByTestId('next-scan-event')).toBeDisabled()
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
  expect(current.scan.cancelStart).not.toHaveBeenCalled()
  expect(current.owner.close).not.toHaveBeenCalled()
  await act(async () => { pull.resolve({ advertisement: advertisement() }) })
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/sample-0/)
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(1)
})

it.each(['start', 'pull'] as const)('closes during pending scan %s and ignores old-owner late results in the new run', async pending => {
  const startWait = deferred<BlengScanCommandResult>()
  const pull = deferred<BlengScanNext>()
  if (pending === 'start') current.scan.waitUntilStarted.mockReturnValue(startWait.promise)
  else current.scan.nextEvent.mockReturnValue(pull.promise)
  await startedApp()
  await press('start-scan')
  if (pending === 'pull') await press('next-scan-event')
  expect(screen.getByTestId('close-session')).toBeEnabled()
  await press('close-session')
  expect(current.owner.close).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
  expect(current.scan.cancelStart).not.toHaveBeenCalled()
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^ownerClosed$/)
  const replacement = fixture()
  createOwner.mockReturnValue(replacement.owner as unknown as BlengSessionOwner)
  await press('start-session')
  await press('start-scan')
  await act(async () => {
    startWait.resolve({ failure: rejected })
    pull.resolve({ advertisement: advertisement(999) })
  })
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^running$/)
  expect(screen.getByTestId('scan-samples')).not.toHaveTextContent(/sample-999/)
  expect(screen.queryByTestId('scan-error')).toBeNull()
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-run')).toHaveTextContent(/^Run 2$/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/Rejected/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/scan-pull/)
})

it('preserves failed close and the owner for retry without silently stopping the scan', async () => {
  current.owner.close.mockRejectedValueOnce(new Error('close blocked'))
  await startedApp()
  await press('start-scan')
  await press('close-session')
  expect(screen.getByTestId('session-error')).toHaveTextContent('close blocked')
  expect(screen.getByTestId('scan-state')).not.toHaveTextContent(/^ownerClosed$/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  await press('close-session')
  expect(current.owner.close).toHaveBeenCalledTimes(2)
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
})

it('cancels start and stop independently from their original pending waits, not as rollback', async () => {
  const startWait = deferred<BlengScanCommandResult>()
  const stopWait = deferred<BlengScanCommandResult>()
  current.scan.waitUntilStarted.mockReturnValue(startWait.promise)
  current.stop.result.mockReturnValue(stopWait.promise)
  await startedApp()
  await press('start-scan')
  await press('cancel-scan-start')
  expect(current.scan.cancelStart).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^starting$/)
  await press('stop-scan')
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopPending$/)
  await press('cancel-scan-stop')
  expect(current.stop.cancel).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopPending$/)
  await act(async () => { startWait.resolve({}) })
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^stopPending$/)
  await act(async () => { stopWait.resolve({ failure: rejected }) })
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
})

it('bounds advertisement summaries and shared evidence, disclosing total observations and omitted samples', async () => {
  await startedApp()
  await press('start-scan')
  for (let index = 0; index < 40; ++index) {
    current.scan.nextEvent.mockResolvedValueOnce({ advertisement: advertisement(index) })
    await press('next-scan-event')
  }
  expect(screen.getByTestId('scan-count')).toHaveTextContent(/40 advertisements observed; 32 samples omitted/)
  expect(screen.getByTestId('scan-samples')).not.toHaveTextContent(/name="sample-31"/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/name="sample-32"/)
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/name="sample-39"/)
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-omitted')).toHaveTextContent(/11 earlier observations omitted/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-pull: succeeded/)
})

it('handles typed terminal events without treating failure as a requested stop or RF verdict', async () => {
  current.scan.nextEvent.mockResolvedValue({ ended: { reason: 'failed', failure: rejected } })
  await startedApp()
  await press('start-scan')
  await press('next-scan-event')
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/operation\/Rejected/)
  expect(screen.getByTestId('next-scan-event')).toBeDisabled()
  expect(screen.getByTestId('scan-ended')).toHaveTextContent(/failed/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-pull: failed — operation\/Rejected/)
})

it('validates only safe positive integer timeout syntax locally, leaving the actual limit to typed native input failure', async () => {
  const inputFailure: BlengScanFailureInfo = {
    domain: 'input', code: 'testTimeoutRejected', message: 'timeout rejected by native input validation',
  }
  current.scan.waitUntilStarted.mockResolvedValue({ failure: inputFailure })
  await startedApp()
  for (const invalid of ['0', '-1', '1.5', '9007199254740992']) {
    fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), invalid)
    expect(screen.getByTestId('start-scan')).toBeDisabled()
    fireEvent.press(screen.getByTestId('start-scan'))
  }
  expect(current.owner.beginScan).not.toHaveBeenCalled()
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '2147483648')
  expect(screen.getByTestId('start-scan')).toBeEnabled()
  expect(screen.getByText(/Rust validates the actual timeout limit/)).toBeTruthy()
  await press('start-scan')
  expect(current.scan.waitUntilStarted).toHaveBeenCalledWith(2147483648)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/input\/testTimeoutRejected/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/scan-start-wait: failed — input\/testTimeoutRejected/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/scan-start-wait: succeeded/)
})

async function pendingScanCalls() {
  const startWait = deferred<BlengScanCommandResult>()
  const pull = deferred<BlengScanNext>()
  const stopWait = deferred<BlengScanCommandResult>()
  const cancelStart = deferred<BlengScanCommandResult>()
  const cancelStop = deferred<BlengScanCommandResult>()
  current.scan.waitUntilStarted.mockReturnValue(startWait.promise)
  current.scan.nextEvent.mockReturnValue(pull.promise)
  current.stop.result.mockReturnValue(stopWait.promise)
  current.scan.cancelStart.mockReturnValue(cancelStart.promise)
  current.stop.cancel.mockReturnValue(cancelStop.promise)
  await startedApp()
  await press('start-scan')
  await press('next-scan-event')
  await press('stop-scan')
  await press('cancel-scan-start')
  await press('cancel-scan-stop')
  return { startWait, pull, stopWait, cancelStart, cancelStop }
}
const lateResults = ['receipts', 'typed failures', 'ended', 'promise rejections'] as const
function finishScanCalls(calls: Awaited<ReturnType<typeof pendingScanCalls>>, kind: typeof lateResults[number]) {
  if (kind === 'promise rejections') {
    for (const call of Object.values(calls)) call.reject(new Error('late bridge rejection'))
  } else {
    const command = kind === 'typed failures' ? { failure: rejected } : {}
    calls.startWait.resolve(command)
    calls.stopWait.resolve(command)
    calls.cancelStart.resolve(command)
    calls.cancelStop.resolve(command)
    calls.pull.resolve(kind === 'typed failures' ? { failure: rejected }
      : kind === 'ended' ? { ended: { reason: 'failed', failure: rejected } }
        : { advertisement: advertisement(999) })
  }
}

describe.each(['succeeds', 'fails', 'closesWithFailure'] as const)('when owner close %s', closeResult => {
  it.each(lateResults)('settles original action evidence for late %s without applying stale scan results', async kind => {
    if (closeResult === 'fails') current.owner.close.mockRejectedValueOnce(new Error('close blocked'))
    if (closeResult === 'closesWithFailure') current.owner.close.mockResolvedValueOnce({
      ownerClosed: true, failure: { code: 'retirement_cleanup', message: 'Cleanup failed.' },
    })
    const calls = await pendingScanCalls()
    await press('close-session')
    const phase = closeResult === 'fails' ? 'stopPending' : 'ownerClosed'
    expect(screen.getByTestId('scan-state')).toHaveTextContent(phase)
    await press('workbench-evidence')
    for (const action of ['scan-start-wait', 'scan-pull', 'scan-stop', 'scan-cancel-start', 'scan-cancel-stop']) {
      expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(new RegExp(`${action}: pending`))
    }
    await act(async () => { finishScanCalls(calls, kind) })
    for (const action of ['scan-start-wait', 'scan-pull', 'scan-stop', 'scan-cancel-start', 'scan-cancel-stop']) {
      const failed = kind === 'typed failures' || kind === 'promise rejections' || (kind === 'ended' && action === 'scan-pull')
      const expected = failed ? kind === 'promise rejections'
        ? 'failed — late bridge rejection' : 'failed — operation/Rejected'
        : 'succeeded'
      expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(new RegExp(`${action}: ${expected}`))
      expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(new RegExp(`${action}: pending`))
    }
    expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(new RegExp(`close: ${closeResult === 'succeeds' ? 'succeeded' : closeResult === 'fails' ? 'failed — close blocked' : 'failed — retirement_cleanup: Cleanup failed.'}`))
    expect(screen.getByTestId('scan-state')).toHaveTextContent(phase)
    expect(screen.getByTestId('scan-count')).toHaveTextContent(/^0 advertisements observed;/)
    expect(screen.getByTestId('scan-samples')).not.toHaveTextContent(/sample-999/)
    expect(screen.queryByTestId('scan-error')).toBeNull()
    expect(screen.queryByTestId('scan-ended')).toBeNull()
    expect(current.scan.beginStop).toHaveBeenCalledTimes(1)
    expect(current.stop.wait).toHaveBeenCalledTimes(1)
    expect(current.scan.cancelStart).toHaveBeenCalledTimes(1)
    expect(current.stop.cancel).toHaveBeenCalledTimes(1)
    expect(current.owner.close).toHaveBeenCalledTimes(1)
    if (closeResult === 'fails') expect(screen.getByTestId('session-error')).toHaveTextContent('close blocked')
  })
})

it('does not change replacement session evidence or scan when all old action promises settle', async () => {
  const calls = await pendingScanCalls()
  await press('close-session')
  const replacement = fixture()
  createOwner.mockReturnValue(replacement.owner as unknown as BlengSessionOwner)
  await press('start-session')
  await press('start-scan')
  await press('workbench-evidence')
  const before = screen.getByTestId('lifecycle-evidence').props.children
  await act(async () => {
    calls.startWait.resolve({ failure: rejected })
    calls.pull.resolve({ advertisement: advertisement(999) })
    calls.stopWait.resolve({})
    calls.cancelStart.reject(new Error('old cancel bridge rejection'))
    calls.cancelStop.resolve({ failure: rejected })
  })
  expect(screen.getByTestId('evidence-run')).toHaveTextContent(/^Run 2$/)
  expect(screen.getByTestId('lifecycle-evidence').props.children).toEqual(before)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^running$/)
  expect(screen.getByTestId('scan-count')).toHaveTextContent(/^0 advertisements observed;/)
  expect(screen.getByTestId('scan-samples')).not.toHaveTextContent(/sample-999/)
  expect(screen.queryByTestId('scan-error')).toBeNull()
  expect(screen.queryByTestId('scan-ended')).toBeNull()
  expect(replacement.scan.beginStop).not.toHaveBeenCalled()
  expect(replacement.scan.cancelStart).not.toHaveBeenCalled()
  expect(replacement.stop.cancel).not.toHaveBeenCalled()
})
