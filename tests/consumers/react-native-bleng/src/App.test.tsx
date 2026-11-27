import React from 'react'
import { act, fireEvent, render, screen, within } from '@testing-library/react-native'
import * as safeAreaContext from 'react-native-safe-area-context'
import { createBlengCentral } from '@blehub/react-native-bleng'
import type { BlengAdapterSnapshot, BlengCloseResult, BlengCentral, BlengInitializeResult } from '@blehub/react-native-bleng'
import App from './App'

// Only the native factory is replaced: the pure-JS layer (`pendingOperation`) is the real one,
// so the App's own error and ledger text is exercised exactly as it ships.
jest.mock('@blehub/react-native-bleng', () => ({
  ...jest.requireActual('@blehub/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))

const createCentral = jest.mocked(createBlengCentral)
let initialize: jest.Mock<Promise<BlengInitializeResult>, []>
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
  initialize = jest.fn().mockResolvedValue({ snapshot })
  close = jest.fn().mockResolvedValue({ closed: true })
  createCentral.mockReturnValue({ initialize, close } as unknown as BlengCentral)
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
  let finish!: (value: BlengInitializeResult) => void
  initialize.mockReturnValue(new Promise(resolve => { finish = resolve }))
  try {
    render(<App />)
    const provider = screen.UNSAFE_getByType(safeAreaContext.SafeAreaProvider)
    const safeArea = screen.UNSAFE_getByType(safeAreaContext.SafeAreaView)
    expect(provider.props.initialMetrics).toEqual(metrics)
    expect(safeArea.props.edges).toEqual(['top', 'right', 'bottom', 'left'])
    expect(safeArea.props.mode).toBe('padding')
    expect(within(safeArea).getByTestId('formal-host-title')).toHaveTextContent('BLEHUB FORMAL RN CONSUMER')
    expect(within(safeArea).getByTestId('session-state')).toHaveTextContent('idle')
    expect(within(screen.getByTestId('workbench-content')).queryByTestId('workbench-page')).toBeNull()
    for (const view of ['evidence', 'current', 'tests']) {
      fireEvent.press(within(safeArea).getByTestId(`workbench-${view}`))
      expect(within(safeArea).getByTestId('workbench-page')).toBeVisible()
      if (view === 'current') {
        expect(within(safeArea).getByTestId('command-console')).toBeVisible()
        expect(within(screen.getByTestId('workbench-content')).queryByTestId('command-console')).toBeNull()
      }
    }
    expect(createCentral).not.toHaveBeenCalled()

    fireEvent.press(screen.getByTestId('start-session'))
    await press('workbench-evidence')
    await press('workbench-tests')
    await act(async () => { finish({ snapshot }) })
    expect(screen.UNSAFE_getByType(safeAreaContext.SafeAreaProvider)).toBe(provider)
    expect(screen.UNSAFE_getByType(safeAreaContext.SafeAreaView)).toBe(safeArea)
    expect(within(safeArea).getByTestId('formal-host-title')).toBeVisible()
    expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Tests$/)
    expect(screen.getByTestId('session-state')).toHaveTextContent('started')
    expect(createCentral).toHaveBeenCalledTimes(1)
    expect(initialize).toHaveBeenCalledTimes(1)
    expect(close).not.toHaveBeenCalled()
    await press('workbench-evidence')
    expect(within(safeArea).getByTestId('lifecycle-evidence')).toHaveTextContent(/start: succeeded/)
  } finally {
    insetsReplacement.restore()
    frameReplacement.restore()
  }
})

it('distinguishes navigation handler entry from page commit without creating a run', async () => {
  const beginScan = jest.fn()
  createCentral.mockReturnValue({ initialize, close, beginScan } as unknown as BlengCentral)
  const navigationTrace = (): string[] => closeTrace.mock.calls
    .map(([message]) => String(message))
    .filter(message => message.startsWith('BLEHUB_FORMAL_NAV '))
    .map(message => {
      expect(message).toMatch(/^BLEHUB_FORMAL_NAV phase=(request|commit) page=(Tests|Current run|Evidence) monotonicMs=\d+(?:\.\d+)?$/)
      return message.replace(/ monotonicMs=.*/, '')
    })
  render(<App />)
  expect(navigationTrace()).toEqual(['BLEHUB_FORMAL_NAV phase=commit page=Tests'])
  await press('workbench-evidence')
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(navigationTrace()).toEqual([
    'BLEHUB_FORMAL_NAV phase=commit page=Tests',
    'BLEHUB_FORMAL_NAV phase=request page=Evidence',
    'BLEHUB_FORMAL_NAV phase=commit page=Evidence',
  ])
  await press('workbench-evidence')
  expect(navigationTrace().slice(3)).toEqual(['BLEHUB_FORMAL_NAV phase=request page=Evidence'])
  await press('workbench-current')
  await press('workbench-tests')
  expect(navigationTrace().slice(4)).toEqual([
    'BLEHUB_FORMAL_NAV phase=request page=Current run',
    'BLEHUB_FORMAL_NAV phase=commit page=Current run',
    'BLEHUB_FORMAL_NAV phase=request page=Tests',
    'BLEHUB_FORMAL_NAV phase=commit page=Tests',
  ])
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Tests$/)
  expect(createCentral).not.toHaveBeenCalled()
  expect(initialize).not.toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
  expect(beginScan).not.toHaveBeenCalled()
})

it.each(['request', 'commit'] as const)('keeps navigation and pending ownership intact when %s diagnostics fail', async phase => {
  closeTrace.mockImplementation((message: unknown) => {
    if (String(message).startsWith(`BLEHUB_FORMAL_NAV phase=${phase} `)) {
      throw new Error('navigation diagnostic sink unavailable')
    }
  })
  const beginScan = jest.fn()
  createCentral.mockReturnValue({ initialize, close, beginScan } as unknown as BlengCentral)
  let finish!: (value: BlengInitializeResult) => void
  initialize.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  await press('workbench-evidence')
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(createCentral).not.toHaveBeenCalled()
  await press('workbench-tests')
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^starting$/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: pending/)
  await act(async () => { finish({ snapshot }) })
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^started$/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: succeeded/)
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(initialize).toHaveBeenCalledTimes(1)
  expect(close).not.toHaveBeenCalled()
  expect(beginScan).not.toHaveBeenCalled()
  expect(closeTrace.mock.calls.some(([message]) => String(message).startsWith(`BLEHUB_FORMAL_NAV phase=${phase} `))).toBe(true)
})

it('publishes one pinned status projection that tracks state and page together', async () => {
  render(<App />)
  const status = (): Record<string, string> =>
    Object.fromEntries(screen.getByTestId('run-status').props.children.split(';')
      .map((field: string) => {
        const index = field.indexOf('=')
        return [field.slice(0, index), field.slice(index + 1)]
      }))
  // Pinned: outside the scrolled content, so a scene never pays a drag for it.
  expect(within(screen.getByTestId('workbench-content')).queryByTestId('run-status')).toBeNull()
  expect(status()).toMatchObject({ state: 'idle', page: 'Tests', scan: 'idle' })
  await press('workbench-current')
  expect(status()).toMatchObject({ state: 'idle', page: 'Current run' })
  // The scene's declared request is projected, not inferred from a label.
  expect(status()).toMatchObject({
    scanTimeout: '5000', scanFilter: 'none',
    // Nothing acts until a characteristic is declared, and the declaration is by UUID.
    gattTarget: 'none', gattMatch: 'none', gattProps: 'none',
  })
  await act(async () => {
    fireEvent.changeText(screen.getByTestId('gatt-target'), 'FFF20000-0000-4000-8000-000000000000')
  })
  // Declared but not yet discovered: the projection says so instead of guessing.
  expect(status()).toMatchObject({
    gattTarget: 'fff20000-0000-4000-8000-000000000000', gattMatch: 'none',
  })
  await act(async () => { fireEvent.press(screen.getByTestId('start-session')) })
  await press('workbench-evidence')
  expect(status()).toMatchObject({ state: 'started', page: 'Evidence' })
  expect(createCentral).toHaveBeenCalledTimes(1)
})

it('browsing creates no native owner, and initialization facts do not imply radio readiness', async () => {
  const beginScan = jest.fn()
  createCentral.mockReturnValue({ initialize, close, beginScan } as unknown as BlengCentral)
  render(<App />)
  const navigation = [
    ['tests', 'Tests'], ['current', 'Current run'], ['evidence', 'Evidence'],
  ] as const
  const visits = [navigation[0], navigation[2], navigation[1], navigation[0]]
  for (const [step, [view, title]] of visits.entries()) {
    if (step > 0) await press(`workbench-${view}`)
    expect(screen.getByTestId('workbench-page')).toHaveTextContent(new RegExp(`^${title}$`))
    expect(screen.getByTestId('session-state')).toHaveTextContent(/^idle$/)
    expect(screen.getAllByRole('button', { selected: true })).toEqual([
      screen.getByTestId(`workbench-${view}`),
    ])
    for (const [identifier] of navigation) {
      const button = screen.getByTestId(`workbench-${identifier}`)
      expect(button).toBeEnabled()
      if (identifier === view) expect(button).toBeSelected()
      else expect(button).not.toBeSelected()
    }
    const content = within(screen.getByTestId('workbench-content'))
    // Session control is pinned on every page. This is a contract, not a layout detail: a
    // scene's first command is `start-session`, issued before any navigation, so a Start
    // button reachable only through the scroll container would make every scene depend on
    // "the first content node happens to be in the viewport".
    const pinned = within(screen.getByTestId('session-controls'))
    expect(content.queryByTestId('start-session')).toBeNull()
    expect(content.queryByTestId('close-session')).toBeNull()
    expect(pinned.getByTestId('start-session')).toBeEnabled()
    expect(pinned.getByTestId('close-session')).toBeEnabled()
    if (view === 'evidence') {
      expect(content.getByTestId('lifecycle-evidence')).toHaveTextContent(/^No run has been requested\.$/)
    } else {
      expect(content.queryByTestId('lifecycle-evidence')).toBeNull()
    }
    if (view === 'current') {
      expect(content.getByText(/Switching views leaves this run and pending operations intact\./)).toBeVisible()
      // The run's phases are content a human reads; the machine reads the projection.
      expect(within(screen.getByTestId('workbench-content')).getByTestId('scan-state')).toHaveTextContent(/^idle$/)
      expect(screen.getByTestId('run-status')).toHaveTextContent(/scan=idle/)
      for (const identifier of ['start-scan', 'next-scan-event', 'stop-scan']) {
        expect(screen.getByTestId(identifier)).toBeDisabled()
      }
    } else {
      expect(content.queryByTestId('close-session')).toBeNull()
      expect(screen.queryByTestId('command-console')).toBeNull()
      if (view === 'tests') {
        expect(content.getByText(/Session lifecycle observes initialization, then explicit close\./)).toBeVisible()
      }
    }
    expect(screen.queryByTestId('evidence-run')).toBeNull()
    expect(screen.queryByTestId('adapter-state')).toBeNull()
    expect(createCentral).not.toHaveBeenCalled()
    expect(initialize).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    expect(beginScan).not.toHaveBeenCalled()
  }

  await press('start-session')
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(initialize).toHaveBeenCalledTimes(1)
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
    initialize.mockResolvedValue({
      snapshot: {
        state,
        scanAuthorization: 'notRequired',
        connectionAuthorization: 'notDetermined',
      },
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
  initialize.mockResolvedValueOnce({ snapshot: { ...snapshot, name: '' } }).mockResolvedValueOnce({
    snapshot: { state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'denied' },
  })
  render(<App />)
  await press('start-session')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent(/^$/)
  await press('close-session')
  await press('start-session')
  expect(createCentral).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOn')
  expect(screen.getByTestId('connection-authorization')).toHaveTextContent('denied')
  expect(screen.getByTestId('adapter-name')).toHaveTextContent('not reported')
})

it('serializes UI actions while initialization is pending, without a late start overriding close', async () => {
  let finish!: (value: BlengInitializeResult) => void
  initialize.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('session-state')).toHaveTextContent('starting')
  expect(screen.getByTestId('start-session')).toBeDisabled()
  expect(screen.getByTestId('close-session')).toBeDisabled()
  fireEvent.press(screen.getByTestId('start-session'))
  fireEvent.press(screen.getByTestId('close-session'))
  expect(initialize).toHaveBeenCalledTimes(1)
  expect(close).not.toHaveBeenCalled()
  await act(async () => { finish({ snapshot }) })
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
})

it('preserves errors and retains the owner for explicit cleanup instead of manufacturing a snapshot', async () => {
  initialize.mockResolvedValue({ failure: { domain: 'lifecycle', code: 'bleng.timeout', message: 'startup failed' } })
  render(<App />)
  await press('start-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('failed')
  expect(screen.getByTestId('session-error')).toHaveTextContent('startup failed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(1)
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
})

it('keeps the same owner after a nonterminal close receipt and permits explicit retry', async () => {
  close.mockResolvedValueOnce({ closed: false, failure: { domain: 'lifecycle', code: 'supervisor_busy', message: 'Close has not run.' } })
  render(<App />)
  await press('start-session')
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent('failed')
  expect(screen.getByTestId('session-error')).toHaveTextContent('supervisor_busy: Close has not run.')
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  expect(createCentral).toHaveBeenCalledTimes(1)
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
})

it('retires controls on a terminal cleanup failure without calling it successful or retrying cleanup', async () => {
  close.mockResolvedValue({ closed: true, failure: {
    domain: 'lifecycle', code: 'native_resources_not_zero', message: 'Native resources remained after retirement.',
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
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(1)
  await press('start-session')
  expect(createCentral).toHaveBeenCalledTimes(2)
  expect(initialize).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^started$/)
})

it('can explicitly create a fresh owner after failed initialization and terminal close, preserving both failures until then', async () => {
  initialize.mockRejectedValueOnce(new Error('initialization failed'))
  close.mockResolvedValueOnce({ closed: true, failure: {
    domain: 'lifecycle', code: 'no_active_session', message: 'Supervisor had no active session to close.',
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
  expect(createCentral).toHaveBeenCalledTimes(2)
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^started$/)
})

it('does not treat an unconfirmed close without failure details as success', async () => {
  close.mockResolvedValueOnce({ closed: false })
  render(<App />)
  await press('start-session')
  await press('close-session')
  expect(screen.getByTestId('session-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('session-error')).toHaveTextContent(/did not confirm owner closure/)
  expect(screen.getByTestId('adapter-state')).toHaveTextContent('poweredOff')
  await press('close-session')
  expect(close).toHaveBeenCalledTimes(2)
  expect(createCentral).toHaveBeenCalledTimes(1)
})

it('keeps a pending start alive across views and records its result without stealing navigation', async () => {
  let finish!: (value: BlengInitializeResult) => void
  initialize.mockReturnValue(new Promise(resolve => { finish = resolve }))
  render(<App />)
  fireEvent.press(screen.getByTestId('start-session'))
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Current run$/)

  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: pending/)
  await press('workbench-tests')
  expect(screen.getByTestId('start-session')).toBeDisabled()
  fireEvent.press(screen.getByTestId('start-session'))
  await press('workbench-evidence')
  await act(async () => { finish({ snapshot }) })

  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('session-state')).toHaveTextContent('started')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: succeeded/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOff')
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(initialize).toHaveBeenCalledTimes(1)
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
  expect(closeTrace.mock.calls.filter(([message]) => String(message).startsWith('BLEHUB_FORMAL_CLOSE '))
    .map(([message]) => String(message).split(' ')[1])).toEqual([
    'phase=js.close.call', 'phase=js.close.promise',
  ])
  await act(async () => { finish({ closed: true }) })
  expect(closeTrace.mock.calls.filter(([message]) => String(message).startsWith('BLEHUB_FORMAL_CLOSE '))
    .map(([message]) => String(message).split(' ')[1])).toEqual([
    'phase=js.close.call', 'phase=js.close.promise', 'phase=js.close.receipt', 'phase=js.close.finally',
  ])
  expect(closeTrace.mock.calls.every(([message]) => !String(message).includes(snapshot.name!))).toBe(true)

  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Evidence$/)
  expect(screen.getByTestId('session-state')).toHaveTextContent('closed')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/close: succeeded/)
  await press('workbench-current')
  expect(screen.queryByTestId('adapter-state')).toBeNull()
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(initialize).toHaveBeenCalledTimes(1)
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
  expect(createCentral).toHaveBeenCalledTimes(1)
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
  expect(createCentral).toHaveBeenCalledTimes(1)

  initialize.mockResolvedValue({ snapshot: { ...snapshot, state: 'poweredOn', name: undefined } })
  await press('start-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('evidence-run')).toHaveTextContent(/^Run 2$/)
  expect(screen.getByTestId('evidence-adapter-state')).toHaveTextContent('poweredOn')
  expect(screen.getByTestId('evidence-adapter-name')).toHaveTextContent('not reported')
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/close: succeeded/)
  expect(createCentral).toHaveBeenCalledTimes(2)
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
  expect(createCentral).toHaveBeenCalledTimes(1)
  expect(close).toHaveBeenCalledTimes(33)
})

it('records construction failure without inventing initialization or cleanup evidence', async () => {
  createCentral.mockImplementationOnce(() => { throw new Error('native construction failed') })
  render(<App />)
  await press('start-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/start: failed — native construction failed/)
  expect(screen.queryByTestId('evidence-adapter-state')).toBeNull()
  await press('workbench-current')
  await press('close-session')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/close: succeeded/)
  expect(initialize).not.toHaveBeenCalled()
  expect(close).not.toHaveBeenCalled()
})
