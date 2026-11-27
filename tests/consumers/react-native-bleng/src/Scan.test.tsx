import React from 'react'
import { act, fireEvent, render, screen, within } from '@testing-library/react-native'
import { createBlengCentral } from '@blehub/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengPeripheral, BlengScan, BlengScanAdvertisement,
  BlengReceipt, BlengFailureInfo, BlengScanNext, BlengScanStop,
  BlengCentral, BlengInitializeResult,
} from '@blehub/react-native-bleng'
import App from './App'

// Only the native factory is replaced: the pure-JS layer (`pendingOperation`) is the real one,
// so the App's own error and ledger text is exercised exactly as it ships.
jest.mock('@blehub/react-native-bleng', () => ({
  ...jest.requireActual('@blehub/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))
const createCentral = jest.mocked(createBlengCentral)
type RenderedNode = ReturnType<typeof screen.getByTestId>
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
const timeout: BlengFailureInfo = {
  domain: 'observation', code: 'Timeout', message: 'observation deadline',
}
const rejected: BlengFailureInfo = {
  domain: 'operation', code: 'Rejected', message: 'native refused',
  platform: { platform: 'apple', code: '15', name: 'ATT', message: 'not permitted' },
}
const invalidTimeout: BlengFailureInfo = {
  domain: 'input', code: 'invalid_timeout', message: 'timeout rejected by Rust',
}
const stopTimeout: BlengFailureInfo = {
  domain: 'operation', code: 'bleng.timeout', message: 'stop result deadline',
}
function stopFixture() {
  let consumed = false
  const result = jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({})
  return {
    result,
    wait: jest.fn(async (timeoutMs: number): Promise<BlengReceipt> => {
      if (consumed) return { failure: {
        domain: 'operation', code: 'bleng.operation_result_consumed', message: 'result already consumed',
      } }
      // Model Rust's single-consumer slot, including timeout/failure and pending waits.
      consumed = true
      const outcome = await result(timeoutMs)
      if (outcome.failure?.domain === 'input' && outcome.failure.code === 'invalid_timeout') consumed = false
      return outcome
    }),
    cancel: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
  }
}
function fixture() {
  const stop = stopFixture()
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({ failure: timeout }),
    beginStop: jest.fn().mockReturnValue({ stop: stop as unknown as BlengScanStop }),
  }
  const central = {
    initialize: jest.fn().mockResolvedValue({ snapshot }),
    close: jest.fn().mockResolvedValue({ closed: true }),
    beginScan: jest.fn().mockReturnValue({ scan: scan as unknown as BlengScan }),
  }
  return { central, scan, stop }
}
let current: ReturnType<typeof fixture>
let closeTrace: jest.SpyInstance
beforeEach(() => {
  jest.clearAllMocks()
  closeTrace = jest.spyOn(console, 'info').mockImplementation(() => {})
  current = fixture()
  createCentral.mockReturnValue(current.central as unknown as BlengCentral)
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
  const ownerStart = deferred<BlengInitializeResult>()
  current.central.initialize.mockReturnValue(ownerStart.promise)
  const scanStart = deferred<BlengReceipt>()
  current.scan.waitUntilStarted.mockReturnValue(scanStart.promise)
  render(<App />)
  await press('workbench-current')
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  expect(createCentral).not.toHaveBeenCalled()
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  await act(async () => { ownerStart.resolve({ snapshot }) })
  expect(current.central.beginScan).not.toHaveBeenCalled()
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '1234')
  await press('start-scan')
  expect(current.central.beginScan).toHaveBeenCalledWith({ serviceUuids: [], allowDuplicates: false })
  expect(current.scan.waitUntilStarted).toHaveBeenCalledWith(1234)
  expect(screen.getByTestId('scan-accepted')).toHaveTextContent(/accepted/)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^starting$/)
  expect(current.scan.nextEvent).not.toHaveBeenCalled()
  fireEvent.press(screen.getByTestId('start-scan'))
  expect(current.central.beginScan).toHaveBeenCalledTimes(1)
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

it.each(['running', 'failed end'] as const)('anchors live scan controls outside scrolling samples with consistent %s evidence', async outcome => {
  const longName = 'x'.repeat(160)
  current.scan.nextEvent.mockImplementation(async () => ({ advertisement: {
    ...advertisement(), localName: longName,
    advertisedServices: Array.from({ length: 8 }, (_, i) => `${i}0000000-0000-4000-8000-000000000000`),
  } }))
  await startedApp()
  await press('start-scan')
  for (let i = 0; i < 8; i += 1) await press('next-scan-event')
  if (outcome === 'failed end') {
    current.scan.nextEvent.mockResolvedValueOnce({ ended: { reason: 'failed', failure: rejected } })
    await press('next-scan-event')
  }
  const samples = screen.getByTestId('scan-samples').props.children
  expect(samples.split('\n\n')).toHaveLength(8)
  expect(samples).toContain(longName)
  expect(screen.getByTestId('scan-count')).toHaveTextContent(/^8 advertisements observed; 0 samples omitted/)
  expect(screen.getByTestId('scan-state')).toHaveTextContent(outcome === 'running' ? /^running$/ : /^failed$/)
  if (outcome === 'failed end') {
    expect(screen.getByTestId('scan-error')).toHaveTextContent(/operation\/Rejected: native refused \[apple\/15 ATT: not permitted\]/)
    expect(screen.getByTestId('scan-ended')).toHaveTextContent(/^ended: failed$/)
  }
  const summaryIds = ['scan-state', 'scan-accepted', 'scan-resource', 'scan-count', 'scan-samples',
    ...(outcome === 'failed end' ? ['scan-error', 'scan-ended'] : [])]
  const summary = summaryIds.map(id => screen.getByTestId(id).props.children)
  // Commands are pinned; every label a human reads stays in the scrolled content. That
  // split is the design: a scene must never scroll to reach an action, and content may be
  // arbitrarily long because nothing machine-relevant is in it.
  const commandIds = ['start-scan', 'next-scan-event', 'stop-scan', 'connect-peripheral',
    'disconnect-peripheral', 'discover-gatt', 'read-rssi', 'gatt-read', 'gatt-write',
    'gatt-subscribe', 'pull-notification', 'disable-notifications']
  const inputIds = ['gatt-target', 'gatt-write-value', 'scan-observation-timeout',
    'scan-service-filter']
  const labelIds = [...summaryIds, 'scan-filter', 'gatt-summary', 'gatt-rows',
    'gatt-target-status', 'notify-mode', 'notify-count', 'notify-samples']
  for (const page of ['current', 'evidence', 'tests', 'current']) {
    await press(`workbench-${page}`)
    if (page === 'tests') continue
    for (const [index, id] of summaryIds.entries()) {
      expect(screen.getAllByTestId(id)).toHaveLength(1)
      expect(screen.getByTestId(id).props.children).toEqual(summary[index])
    }
    const viewport = screen.getByTestId('scan-sample-viewport')
    expect(viewport.props.nestedScrollEnabled).toBe(true)
    expect(viewport).toHaveStyle({ maxHeight: 160, flexGrow: 0 })
    expect(within(viewport).getByTestId('scan-samples').props.children).toBe(samples)
    const content = screen.getByTestId('workbench-content')
    // The machine surface is pinned and the content is not part of it.
    expect(within(content).queryByTestId('run-status')).toBeNull()
    expect(within(content).queryByTestId('scan-latest')).toBeNull()
    if (page === 'current') {
      expect(screen.getByTestId('run-status').props.children).toContain(
        `scan=${outcome === 'running' ? 'running' : 'failed'}`)
    }
    for (const id of summaryIds) {
      expect(within(content).getByTestId(id)).toBe(screen.getByTestId(id))
    }
    if (page === 'evidence') {
      expect(screen.queryByTestId('command-console')).toBeNull()
      for (const id of [...commandIds, ...inputIds]) expect(screen.queryByTestId(id)).toBeNull()
      continue
    }
    // Everything a human reads is content, and only content.
    for (const id of [...labelIds, 'cancel-scan-start', 'cancel-scan-stop']) {
      expect(within(content).getByTestId(id)).toBe(screen.getByTestId(id))
    }
    const console_ = screen.getByTestId('command-console')
    expect(console_.props.accessible).not.toBe(true)
    expect(within(content).queryByTestId('command-console')).toBeNull()
    for (const id of [...commandIds, ...inputIds]) {
      expect(within(console_).getByTestId(id)).toBe(screen.getByTestId(id))
      expect(within(content).queryByTestId(id)).toBeNull()
    }
    // React containment/order only, not a native viewport or timing measurement.
    const orderedIds = ['scan-primary-controls', 'start-scan', 'next-scan-event', 'stop-scan',
      'link-controls', 'gatt-target', 'gatt-action-strip', 'notify-action-strip']
    expect(console_.findAll((node: RenderedNode) => typeof node.type === 'string' && orderedIds.includes(node.props.testID))
      .map((node: RenderedNode) => node.props.testID)).toEqual(orderedIds)
    for (const row of ['scan-primary-controls', 'link-controls', 'gatt-action-strip',
      'notify-action-strip']) {
      expect(within(console_).getByTestId(row)).toHaveStyle({ flexDirection: 'row', flexWrap: 'wrap' })
    }
  }
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(current.central.initialize).toHaveBeenCalledTimes(1)
  expect(current.central.beginScan).toHaveBeenCalledTimes(1)
  expect(current.scan.waitUntilStarted).toHaveBeenCalledTimes(1)
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(outcome === 'running' ? 8 : 9)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
  expect(current.scan.cancelStart).not.toHaveBeenCalled()
  expect(current.stop.wait).not.toHaveBeenCalled()
  expect(current.stop.cancel).not.toHaveBeenCalled()
  expect(current.central.close).not.toHaveBeenCalled()
})

it('displays structured submission and start failures as failures, never successful starts', async () => {
  current.central.beginScan.mockReturnValueOnce({ failure: rejected })
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
  current.scan.beginStop.mockReturnValueOnce({ stop: current.stop as unknown as BlengScanStop })
    .mockReturnValueOnce({ stop: replacement as unknown as BlengScanStop })
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
    .mockReturnValueOnce({ stop: current.stop as unknown as BlengScanStop })
    .mockImplementationOnce(() => {
      if (kind === 'bridge throw') throw new Error('stop submission bridge rejected')
      return { failure: rejected }
    })
    .mockReturnValueOnce({ stop: replacement as unknown as BlengScanStop })
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
  const completion = deferred<BlengReceipt>()
  const cancellation = deferred<BlengReceipt>()
  const replacement = stopFixture()
  current.stop.result.mockResolvedValue({ failure: stopTimeout })
  replacement.result.mockReturnValue(completion.promise)
  replacement.cancel.mockReturnValue(cancellation.promise)
  current.scan.beginStop.mockReturnValueOnce({ stop: current.stop as unknown as BlengScanStop })
    .mockReturnValueOnce({ stop: replacement as unknown as BlengScanStop })
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
  expect(current.central.close).not.toHaveBeenCalled()
  await act(async () => { pull.resolve({ advertisement: advertisement() }) })
  expect(screen.getByTestId('scan-samples')).toHaveTextContent(/sample-0/)
  expect(current.scan.nextEvent).toHaveBeenCalledTimes(1)
})

it.each(['start', 'pull'] as const)('closes during pending scan %s and ignores old-owner late results in the new run', async pending => {
  const startWait = deferred<BlengReceipt>()
  const pull = deferred<BlengScanNext>()
  if (pending === 'start') current.scan.waitUntilStarted.mockReturnValue(startWait.promise)
  else current.scan.nextEvent.mockReturnValue(pull.promise)
  await startedApp()
  await press('start-scan')
  if (pending === 'pull') await press('next-scan-event')
  expect(screen.getByTestId('close-session')).toBeEnabled()
  await press('close-session')
  expect(current.central.close).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
  expect(current.scan.cancelStart).not.toHaveBeenCalled()
  expect(screen.getByTestId('scan-state')).toHaveTextContent(/^ownerClosed$/)
  const replacement = fixture()
  createCentral.mockReturnValue(replacement.central as unknown as BlengCentral)
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
  current.central.close.mockRejectedValueOnce(new Error('close blocked'))
  await startedApp()
  await press('start-scan')
  await press('close-session')
  expect(screen.getByTestId('session-error')).toHaveTextContent('close blocked')
  expect(screen.getByTestId('scan-state')).not.toHaveTextContent(/^ownerClosed$/)
  expect(screen.getByTestId('scan-resource')).toHaveTextContent(/^Scan\/control retained;/)
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  await press('close-session')
  expect(current.central.close).toHaveBeenCalledTimes(2)
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(current.scan.beginStop).not.toHaveBeenCalled()
})

it('cancels start and stop independently from their original pending waits, not as rollback', async () => {
  const startWait = deferred<BlengReceipt>()
  const stopWait = deferred<BlengReceipt>()
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

it('sends the declared service filter as the scan request and refuses a half-parsed one', async () => {
  await startedApp()
  const filter = screen.getByTestId('scan-service-filter')
  expect(screen.getByTestId('scan-filter')).toHaveTextContent(/^Filter: none \(unfiltered scan\)$/)
  fireEvent.changeText(filter, ' not-a-uuid ')
  expect(screen.getByTestId('scan-filter-error')).toHaveTextContent(/Not a full 128-bit UUID/)
  expect(screen.getByTestId('start-scan')).toBeDisabled()
  fireEvent.press(screen.getByTestId('start-scan'))
  expect(current.central.beginScan).not.toHaveBeenCalled()
  // Case is normalised and duplicates collapse: the request states exactly one UUID.
  fireEvent.changeText(
    filter,
    'FD600000-0000-4000-8000-000000000000, fd600000-0000-4000-8000-000000000000',
  )
  expect(screen.getByTestId('scan-filter')).toHaveTextContent(
    /^Filter: fd600000-0000-4000-8000-000000000000$/,
  )
  expect(screen.getByTestId('start-scan')).toBeEnabled()
  await press('start-scan')
  expect(current.central.beginScan).toHaveBeenCalledWith({
    serviceUuids: ['fd600000-0000-4000-8000-000000000000'], allowDuplicates: false,
  })
})

it('validates only safe positive integer timeout syntax locally, leaving the actual limit to typed native input failure', async () => {
  const inputFailure: BlengFailureInfo = {
    domain: 'input', code: 'testTimeoutRejected', message: 'timeout rejected by native input validation',
  }
  current.scan.waitUntilStarted.mockResolvedValue({ failure: inputFailure })
  await startedApp()
  for (const invalid of ['0', '-1', '1.5', '9007199254740992']) {
    fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), invalid)
    expect(screen.getByTestId('start-scan')).toBeDisabled()
    fireEvent.press(screen.getByTestId('start-scan'))
  }
  expect(current.central.beginScan).not.toHaveBeenCalled()
  fireEvent.changeText(screen.getByTestId('scan-observation-timeout'), '2147483648')
  expect(screen.getByTestId('start-scan')).toBeEnabled()
  // The pinned input is the request; the page explains that the limit is native input's
  // judgement, not the screen's.
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
  const startWait = deferred<BlengReceipt>()
  const pull = deferred<BlengScanNext>()
  const stopWait = deferred<BlengReceipt>()
  const cancelStart = deferred<BlengReceipt>()
  const cancelStop = deferred<BlengReceipt>()
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
    if (closeResult === 'fails') current.central.close.mockRejectedValueOnce(new Error('close blocked'))
    if (closeResult === 'closesWithFailure') current.central.close.mockResolvedValueOnce({
      closed: true, failure: { code: 'retirement_cleanup', message: 'Cleanup failed.' },
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
    expect(current.central.close).toHaveBeenCalledTimes(1)
    if (closeResult === 'fails') expect(screen.getByTestId('session-error')).toHaveTextContent('close blocked')
  })
})

it('does not change replacement session evidence or scan when all old action promises settle', async () => {
  const calls = await pendingScanCalls()
  await press('close-session')
  const replacement = fixture()
  createCentral.mockReturnValue(replacement.central as unknown as BlengCentral)
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
