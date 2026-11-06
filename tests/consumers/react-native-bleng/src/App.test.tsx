import React from 'react'
import { act, fireEvent, render, screen, within } from '@testing-library/react-native'
import * as safeAreaContext from 'react-native-safe-area-context'
import { createBlengSessionOwner } from '@blehub/react-native-bleng'
import type { BlengAdapterSnapshot, BlengCloseResult, BlengSessionOwner } from '@blehub/react-native-bleng'
import App from './App'

jest.mock('@blehub/react-native-bleng', () => ({ createBlengSessionOwner: jest.fn() }))

const createOwner = jest.mocked(createBlengSessionOwner)
let start: jest.Mock<Promise<BlengAdapterSnapshot>, []>
let close: jest.Mock<Promise<BlengCloseResult>, []>
let closeTrace: jest.SpyInstance

const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOff',
  scanAuthorization: 'denied',
  connectionAuthorization: 'restricted',
  name: 'BLE 测试 🦀',
}

beforeEach(() => {
  jest.clearAllMocks()
  closeTrace = jest.spyOn(console, 'info').mockImplementation(() => {})
  start = jest.fn().mockResolvedValue(snapshot)
  close = jest.fn().mockResolvedValue({ ownerClosed: true })
  createOwner.mockReturnValue({ start, close } as unknown as BlengSessionOwner)
})

afterEach(() => { closeTrace.mockRestore() })

async function press(identifier: string): Promise<void> {
  await act(async () => { fireEvent.press(screen.getByTestId(identifier)) })
}

it.each([
  ['Android edge-to-edge', { top: 32, right: 0, bottom: 24, left: 0 }],
  ['iPhone portrait', { top: 59, right: 0, bottom: 34, left: 0 }],
  ['landscape cutout', { top: 0, right: 59, bottom: 21, left: 59 }],
] as const)('keeps all workbench chrome inside %s insets without replacing a pending run', async (_, insets) => {
  const metrics = { frame: { x: 0, y: 0, width: 844, height: 390 }, insets }
  const initialMetrics = safeAreaContext.initialWindowMetrics!
  const frameReplacement = jest.replaceProperty(initialMetrics, 'frame', metrics.frame)
  const insetsReplacement = jest.replaceProperty(initialMetrics, 'insets', metrics.insets)
  let finish!: (value: BlengAdapterSnapshot) => void
  start.mockReturnValue(new Promise(resolve => { finish = resolve }))
  try {
    render(<App />)
    const provider = screen.UNSAFE_getByType(safeAreaContext.SafeAreaProvider)
    const safeArea = screen.UNSAFE_getByType(safeAreaContext.SafeAreaView)
    expect(provider.props.initialMetrics).toEqual(metrics)
    expect(safeArea.props.edges).toEqual(['top', 'right', 'bottom', 'left'])
    expect(safeArea.props.mode).toBe('padding')
    expect(within(safeArea).getByTestId('formal-host-title')).toHaveTextContent('BLEHUB FORMAL RN CONSUMER')
    expect(within(safeArea).getByTestId('session-state')).toHaveTextContent('idle')
    for (const view of ['evidence', 'current', 'tests']) {
      fireEvent.press(within(safeArea).getByTestId(`workbench-${view}`))
      expect(within(safeArea).getByTestId('workbench-page')).toBeVisible()
    }
    expect(createOwner).not.toHaveBeenCalled()

    fireEvent.press(screen.getByTestId('start-session'))
    await press('workbench-evidence')
    await press('workbench-tests')
    await act(async () => { finish(snapshot) })
    expect(screen.UNSAFE_getByType(safeAreaContext.SafeAreaProvider)).toBe(provider)
    expect(screen.UNSAFE_getByType(safeAreaContext.SafeAreaView)).toBe(safeArea)
    expect(within(safeArea).getByTestId('formal-host-title')).toBeVisible()
    expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Tests$/)
    expect(screen.getByTestId('session-state')).toHaveTextContent('started')
    expect(createOwner).toHaveBeenCalledTimes(1)
    expect(start).toHaveBeenCalledTimes(1)
    expect(close).not.toHaveBeenCalled()
    await press('workbench-evidence')
    expect(within(safeArea).getByTestId('lifecycle-evidence')).toHaveTextContent(/start: succeeded/)
  } finally {
    insetsReplacement.restore()
    frameReplacement.restore()
  }
})

it('browsing creates no native owner, and initialization facts do not imply radio readiness', async () => {
  render(<App />)
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Tests$/)
  for (const view of ['evidence', 'current', 'tests']) {
    await press(`workbench-${view}`)
  }
  expect(createOwner).not.toHaveBeenCalled()
  expect(screen.getByTestId('session-state')).toHaveTextContent('idle')
  expect(screen.queryByTestId('adapter-state')).toBeNull()

  await press('start-session')
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(start).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent('started')
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  expect(screen.getByTestId('scan-authorization')).toHaveTextContent('denied')
  expect(screen.getByTestId('connection-authorization')).toHaveTextContent('restricted')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent(snapshot.name!)
  expect(screen.getByTestId('start-session')).toBeDisabled()

  await press('close-session')
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  expect(screen.queryByTestId('session-error')).toBeNull()
})

it.each(['unknown', 'unavailable', 'poweredOn'] as const)(
  'renders %s and independent authorization facts without synthesizing a ready state',
  async state => {
    start.mockResolvedValue({
      state,
      scanAuthorization: 'notRequired',
      connectionAuthorization: 'notDetermined',
    })
    render(<App />)
    await press('start-session')
    expect(screen.getByTestId('adapter-state')).toHaveTextContent(state)
    expect(screen.getByTestId('scan-authorization')).toHaveTextContent('notRequired')
    expect(screen.getByTestId('connection-authorization')).toHaveTextContent('notDetermined')
    expect(screen.getByTestId('adapter-name')).toHaveTextContent('not reported')
    expect(screen.queryByTestId('session-error')).toBeNull()
  },
)

it('preserves a missing versus empty name and does not reuse a retired snapshot on restart', async () => {
  start.mockResolvedValueOnce({ ...snapshot, name: '' }).mockResolvedValueOnce({
    state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'denied',
  })
  render(<App />)
  await press('start-session')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent(/^$/)
  await press('close-session')
  await press('start-session')
  expect(createOwner).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOn')
  expect(screen.getByTestId('connection-authorization')).toHaveTextContent('denied')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent('not reported')
})

it('serializes UI actions while initialization is pending, without a late start overriding close', async () => {
  let finish!: (value: BlengAdapterSnapshot) => void
  start.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('session-state')).toHaveTextContent('starting')
  expect(screen.getByTestId('start-session')).toBeDisabled()
  expect(screen.getByTestId('close-session')).toBeDisabled()
  fireEvent.press(screen.getByTestId('start-session'))
  fireEvent.press(screen.getByTestId('close-session'))
  expect(start).toHaveBeenCalledTimes(1)
  expect(close).not.toHaveBeenCalled()
  await act(async () => { finish(snapshot) })
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
})

it('preserves errors and retains the owner for explicit cleanup instead of manufacturing a snapshot', async () => {
  start.mockRejectedValue(new Error('startup failed'))
  render(<App />)
  await press('start-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('failed')
  expect(screen.getByTestId('session-error')).toHaveTextContent('startup failed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(1)
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
})

it('keeps the same owner after a nonterminal close receipt and permits explicit retry', async () => {
  close.mockResolvedValueOnce({ ownerClosed: false, failure: { code: 'supervisor_busy', message: 'Close has not run.' } })
  render(<App />)
  await press('start-session')
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('failed')
  expect(screen.getByTestId('session-error')).toHaveTextContent('supervisor_busy: Close has not run.')
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  expect(createOwner).toHaveBeenCalledTimes(1)
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
})

it('retires controls on a terminal cleanup failure without calling it successful or retrying cleanup', async () => {
  close.mockResolvedValue({ ownerClosed: true, failure: {
    code: 'native_resources_not_zero', message: 'Native resources remained after retirement.',
  } })
  render(<App />)
  await press('start-session')
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^closedWithError$/)
  expect(screen.getByTestId('session-error')).toHaveTextContent(/native_resources_not_zero/)
  expect(screen.getByTestId('session-retirement-warning')).toHaveTextContent(/not resource-zero/)
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^closedWithError$/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: failed/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/close: succeeded/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
  await press('workbench-tests')
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(1)
  await press('start-session')
  expect(createOwner).toHaveBeenCalledTimes(2)
  expect(start).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^started$/)
})

it('can explicitly create a fresh owner after failed initialization and terminal close, preserving both failures until then', async () => {
  start.mockRejectedValueOnce(new Error('initialization failed'))
  close.mockResolvedValueOnce({ ownerClosed: true, failure: {
    code: 'no_active_session', message: 'Supervisor had no active session to close.',
  } })
  render(<App />)
  await press('start-session')
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: failed — initialization failed/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: failed — no_active_session/)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^closedWithError$/)
  await press('workbench-tests')
  await press('start-session')
  expect(createOwner).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^started$/)
})

it('does not treat an unconfirmed close without failure details as success', async () => {
  close.mockResolvedValueOnce({ ownerClosed: false })
  render(<App />)
  await press('start-session')
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('session-error')).toHaveTextContent(/did not confirm owner closure/)
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(2)
  expect(createOwner).toHaveBeenCalledTimes(1)
})

it('keeps a pending start alive across views and records its result without stealing navigation', async () => {
  let finish!: (value: BlengAdapterSnapshot) => void
  start.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Current run$/)

  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: pending/)
  await press('workbench-tests')
  expect(screen.getByTestId('start-session')).toBeDisabled()
  fireEvent.press(screen.getByTestId('start-session'))
  await press('workbench-evidence')
  await act(async () => { finish(snapshot) })

  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('session-state')).toHaveTextContent('started')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: succeeded/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(start).toHaveBeenCalledTimes(1)
  expect(close).not.toHaveBeenCalled()

  await press('workbench-current')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent(snapshot.name!)
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: succeeded/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
})

it('keeps a pending close alive across views and never creates a replacement owner by browsing', async () => {
  let finish!: (result: BlengCloseResult) => void
  close.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  await press('start-session')
  fireEvent.press(screen.getByTestId('close-session'))
  await press('workbench-tests')
  expect(screen.getByTestId('start-session')).toBeDisabled()
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: pending/)
  expect(closeTrace.mock.calls.map(([message]) => String(message).split(' ')[1])).toEqual([
    'phase=js.close.call', 'phase=js.close.promise',
  ])
  await act(async () => { finish({ ownerClosed: true }) })
  expect(closeTrace.mock.calls.map(([message]) => String(message).split(' ')[1])).toEqual([
    'phase=js.close.call', 'phase=js.close.promise', 'phase=js.close.receipt', 'phase=js.close.finally',
  ])
  expect(closeTrace.mock.calls.every(([message]) => !String(message).includes(snapshot.name!))).toBe(true)

  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: succeeded/)
  await press('workbench-current')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(start).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(1)
})

it('does not let diagnostic logging failure change close delivery or settlement', async () => {
  render(<App />)
  await press('start-session')
  closeTrace.mockImplementation(() => { throw new Error('diagnostic sink unavailable') })
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
})

it('preserves failed cleanup evidence after navigation and a successful explicit retry', async () => {
  close.mockRejectedValueOnce(new Error('retirement failed'))
  render(<App />)
  await press('start-session')
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: failed — retirement failed/)
  await press('workbench-tests')
  await press('workbench-current')
  expect(screen.getByTestId('session-error')).toHaveTextContent('retirement failed')
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  await press('close-session')
  await press('workbench-evidence')

  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: failed — retirement failed/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: succeeded/)
  expect(screen.queryByTestId('session-error')).toBeNull()
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(2)
})

it('keeps the completed run until the next explicit start, without reusing its snapshot', async () => {
  render(<App />)
  await press('start-session')
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-run')).toHaveTextContent(/^Run 1$/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
  await press('workbench-tests')
  expect(createOwner).toHaveBeenCalledTimes(1)

  start.mockResolvedValue({ ...snapshot, state: 'poweredOn', name: undefined })
  await press('start-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-run')).toHaveTextContent(/^Run 2$/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOn')
  expect(screen.getByTestId('evidence-adapter-name')).toHaveTextContent('not reported')
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/close: succeeded/)
  expect(createOwner).toHaveBeenCalledTimes(2)
})

it('bounds repeated failure observations and discloses omitted entries without losing the latest result', async () => {
  close.mockRejectedValue(new Error('cleanup remains blocked'))
  render(<App />)
  await press('start-session')
  for (let attempt = 0; attempt < 33; ++attempt) {
    await press('close-session')
  }
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-omitted')).toHaveTextContent('2 earlier observations omitted from this bounded summary.')
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/1\. start:/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/34\. close: failed — cleanup remains blocked/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
  expect(createOwner).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(33)
})

it('records construction failure without inventing initialization or cleanup evidence', async () => {
  createOwner.mockImplementationOnce(() => { throw new Error('native construction failed') })
  render(<App />)
  await press('start-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: failed — native construction failed/)
  expect(screen.queryByTestId('evidence-adapter-state')).toBeNull()
  await press('workbench-current')
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/close: succeeded/)
  expect(start).not.toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
})
