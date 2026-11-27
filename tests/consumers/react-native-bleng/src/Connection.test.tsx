import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengReceipt, BlengConnect, BlengConnectResult, BlengConnection,
  BlengConnectionNext, BlengDisconnect, BlengFailureInfo, BlengPeripheral, BlengScan,
  BlengScanAdvertisement, BlengScanNext, BlengCentral,
} from '@blengine/react-native-bleng'
import App from './App'

// Only the native factory is replaced: the pure-JS layer (`pendingOperation`) is the real one,
// so the App's own error and ledger text is exercised exactly as it ships.
jest.mock('@blengine/react-native-bleng', () => ({
  ...jest.requireActual('@blengine/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))
const createCentral = jest.mocked(createBlengCentral)
afterEach(() => { closeTrace.mockRestore() })

const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized',
}
const refused: BlengFailureInfo = {
  domain: 'operation', code: 'bleng.busy.peripheral', message: 'peripheral is busy',
}
const disconnected: BlengFailureInfo = {
  domain: 'operation', code: 'bleng.disconnected', message: 'BLE connection was lost',
}
const deadline: BlengFailureInfo = {
  domain: 'observation', code: 'bleng.timeout', message: 'BLeng operation timed out',
}

/** One value the test resolves later; the App's end observer awaits it in the background. */
function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>(next => { resolve = next })
  return { promise, resolve }
}

function fixture() {
  const connectWait = jest.fn<Promise<BlengConnectResult>, [number]>()
  const connectCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})
  const disconnectWait = jest.fn<Promise<BlengReceipt>, [number]>()
  const disconnectCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})
  const beginDisconnect = jest.fn()
  const beginConnect = jest.fn()
  const linkEnd = deferred<BlengConnectionNext>()
  const awaitEnd = jest.fn<Promise<BlengConnectionNext>, [number]>()
    .mockReturnValue(linkEnd.promise)
  const peripheral = { beginConnect } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'connect-target', advertisedServices: [], manufacturerData: [],
        serviceData: [], connectable: true,
      } as BlengScanAdvertisement,
    }),
    beginStop: jest.fn().mockReturnValue({ stop: { wait: jest.fn(), cancel: jest.fn() } }),
  }
  const central = {
    initialize: jest.fn().mockResolvedValue({ snapshot }),
    close: jest.fn().mockResolvedValue({ closed: true }),
    beginScan: jest.fn().mockReturnValue({ scan: scan as unknown as BlengScan }),
  }
  return {
    central, scan, peripheral, beginConnect, beginDisconnect, connectWait, connectCancel,
    disconnectWait, disconnectCancel, awaitEnd, linkEnd,
  }
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

/** Starts the session, scans, and pulls one advertisement so a capability exists. */
async function observedPeripheral() {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
}

function acceptConnect() {
  const connection = {
    beginDisconnect: current.beginDisconnect,
    awaitEnd: current.awaitEnd,
  } as unknown as BlengConnection
  const connect = {
    wait: current.connectWait, cancel: current.connectCancel,
  } as unknown as BlengConnect
  current.beginConnect.mockReturnValue({ connect })
  current.connectWait.mockResolvedValue({ connection })
  const disconnect = {
    wait: current.disconnectWait, cancel: current.disconnectCancel,
  } as unknown as BlengDisconnect
  current.beginDisconnect.mockReturnValue({ disconnect })
  current.disconnectWait.mockResolvedValue({})
}

it('connects only through the newest observed capability, then disconnects through its owner', async () => {
  acceptConnect()
  await observedPeripheral()
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^none$/)
  expect(screen.getByTestId('connect-peripheral')).toBeEnabled()
  expect(screen.getByTestId('disconnect-peripheral')).toBeDisabled()
  expect(current.beginConnect).not.toHaveBeenCalled()

  await press('connect-peripheral')
  expect(current.beginConnect).toHaveBeenCalledTimes(1)
  expect(current.connectWait).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^connected$/)
  expect(screen.getByTestId('connect-peripheral')).toBeDisabled()
  expect(screen.getByTestId('disconnect-peripheral')).toBeEnabled()
  expect(current.beginDisconnect).not.toHaveBeenCalled()

  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/connect: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/connect-wait: succeeded/)
  await press('workbench-current')

  await press('disconnect-peripheral')
  expect(current.beginDisconnect).toHaveBeenCalledTimes(1)
  expect(current.disconnectWait).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  expect(screen.getByTestId('disconnect-peripheral')).toBeDisabled()
  // The observer capability is still valid after the link ends; it is never a device identity.
  expect(screen.getByTestId('connect-peripheral')).toBeEnabled()

  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/disconnect: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/disconnect-wait: succeeded/)
})

it('keeps a refused submission visible and never claims a connection', async () => {
  await observedPeripheral()
  current.beginConnect.mockReturnValue({ failure: refused })
  await press('connect-peripheral')
  expect(current.connectWait).not.toHaveBeenCalled()
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^none$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/operation\/bleng.busy.peripheral/)
  // The machine surface must carry the same failure the panel does: this is the level a scene
  // reads, and a `none` here is a failure the App reported and then hid.
  expect(screen.getByTestId('run-status'))
    .toHaveTextContent(/failure=operation\/bleng.busy.peripheral/)
  expect(screen.getByTestId('disconnect-peripheral')).toBeDisabled()
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/connect: failed/)
  expect(screen.getByTestId('lifecycle-evidence')).not.toHaveTextContent(/connect-wait/)
})

it('reports a failed connect wait without retaining a capability it never received', async () => {
  await observedPeripheral()
  current.beginConnect.mockReturnValue({
    connect: { wait: current.connectWait, cancel: current.connectCancel } as unknown as BlengConnect,
  })
  current.connectWait.mockResolvedValue({ failure: refused })
  await press('connect-peripheral')
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^none$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/operation\/bleng.busy.peripheral/)
  expect(screen.getByTestId('run-status'))
    .toHaveTextContent(/failure=operation\/bleng.busy.peripheral/)
  expect(screen.getByTestId('disconnect-peripheral')).toBeDisabled()
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/connect-wait: failed/)
})

it('treats a failed disconnect as an unknown link and keeps the capability', async () => {
  acceptConnect()
  await observedPeripheral()
  await press('connect-peripheral')
  current.disconnectWait.mockResolvedValue({ failure: disconnected })
  await press('disconnect-peripheral')
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^connected$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/bleng.disconnected/)
  expect(screen.getByTestId('disconnect-peripheral')).toBeEnabled()
})

it('withdraws every link command once the link has ended', async () => {
  // A projection that says `disconnected` must not keep offering commands that can only
  // fail. Measured need: after a peer-initiated end the operator still saw Read/Write/
  // Discover offered on a link that was gone.
  acceptConnect()
  await observedPeripheral()
  await press('connect-peripheral')
  expect(screen.getByTestId('gatt-state')).toHaveTextContent(/^none$/)
  await press('disconnect-peripheral')
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  for (const identifier of [
    'gatt-read', 'gatt-write', 'discover-gatt', 'read-rssi', 'gatt-subscribe',
    'pull-notification', 'disconnect-peripheral',
  ]) {
    expect(screen.getByTestId(identifier)).toBeDisabled()
  }
  // The observer capability is the one that survives: a later scan can still connect.
  expect(screen.getByTestId('connect-peripheral')).toBeEnabled()
})

it('announces a link end nobody here asked for, without sending any command', async () => {
  // The engine reports the terminal on the capability that owns the link, so an end the operator
  // did not cause is an observation rather than the failure of some later command.
  acceptConnect()
  await observedPeripheral()
  await press('connect-peripheral')
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^connected$/)
  expect(current.awaitEnd).toHaveBeenCalledWith(5000)

  await act(async () => { current.linkEnd.resolve({ ended: { reason: 'unrequested' } }) })

  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/link=disconnected;linkEnd=unrequested/)
  // Nothing was submitted to learn this, and nothing needs a dead link any more.
  expect(current.beginDisconnect).not.toHaveBeenCalled()
  for (const identifier of [
    'gatt-read', 'gatt-write', 'discover-gatt', 'read-rssi', 'gatt-subscribe',
    'pull-notification', 'disconnect-peripheral',
  ]) {
    expect(screen.getByTestId(identifier)).toBeDisabled()
  }
  // The failure the run already showed is history, and the end does not erase it.
  expect(screen.queryByTestId('scan-error')).toBeNull()
})

it('keeps the platform\'s own report for an end it did not enter', async () => {
  acceptConnect()
  await observedPeripheral()
  await press('connect-peripheral')

  await act(async () => {
    current.linkEnd.resolve({ ended: { reason: 'unrequested', failure: disconnected } })
  })

  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  expect(screen.getByTestId('scan-error')).toHaveTextContent(/bleng.disconnected/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/linkEnd=unrequested/)
})

it('re-arms after its own deadline instead of calling the deadline an end', async () => {
  acceptConnect()
  const later = deferred<BlengConnectionNext>()
  current.awaitEnd.mockReset()
  current.awaitEnd
    .mockReturnValueOnce(Promise.resolve({ failure: deadline }))
    .mockReturnValueOnce(later.promise)
  await observedPeripheral()
  await press('connect-peripheral')

  // A deadline moves nothing: the link is still the one the operator connected, and the lane is
  // read again with the same bound rather than reported as an end.
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^connected$/)
  expect(current.awaitEnd).toHaveBeenCalledTimes(2)
  expect(current.awaitEnd).toHaveBeenLastCalledWith(5000)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/linkEnd=none/)

  await act(async () => { later.resolve({ ended: { reason: 'unrequested' } }) })
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/linkEnd=unrequested/)
})

it('withdraws the capability when the engine is closing', async () => {
  acceptConnect()
  await observedPeripheral()
  await press('connect-peripheral')
  await press('close-session')
  expect(screen.getByTestId('connect-peripheral')).toBeDisabled()
  expect(screen.getByTestId('disconnect-peripheral')).toBeDisabled()
})
