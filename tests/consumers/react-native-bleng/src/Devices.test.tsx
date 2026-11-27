import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengConnect, BlengConnectResult, BlengConnection,
  BlengDeviceQueryOutcome, BlengDeviceQuerySubmission, BlengFailureInfo, BlengPeripheral,
  BlengScan, BlengScanAdvertisement, BlengScanNext, BlengCentral,
} from '@blengine/react-native-bleng'
import App from './App'

// Only the native factory is replaced: the package's own await layer is the real one, so the
// App's dispatch, ledger and projection are exercised exactly as they ship.
jest.mock('@blengine/react-native-bleng', () => ({
  ...jest.requireActual('@blengine/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))
const createCentral = jest.mocked(createBlengCentral)
afterEach(() => { closeTrace.mockRestore() })

const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized',
}
/** The owner-scoped key the bridge documents as comparable for the same peer. */
const PEER_KEY = 'peer-1'
const unsupported: BlengFailureInfo = {
  domain: 'submission', code: 'bleng.dispatch_rejected.not_supported',
  message: 'this platform has no such inventory',
}

function fixture() {
  const beginDeviceQuery = jest.fn<BlengDeviceQuerySubmission, [string, string[]]>()
  const beginConnect = jest.fn()
  const peripheral = { beginConnect, key: () => PEER_KEY } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<object>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<object>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'inventory-target', advertisedServices: [], manufacturerData: [],
        serviceData: [], connectable: true,
      } as BlengScanAdvertisement,
    }),
    beginStop: jest.fn().mockReturnValue({ stop: { wait: jest.fn(), cancel: jest.fn() } }),
  }
  const central = {
    initialize: jest.fn().mockResolvedValue({ snapshot }),
    close: jest.fn().mockResolvedValue({ closed: true }),
    beginScan: jest.fn().mockReturnValue({ scan: scan as unknown as BlengScan }),
    beginDeviceQuery,
  }
  return { central, scan, beginConnect, beginDeviceQuery }
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

const fact = (key: string): string | null => {
  const text = screen.getByTestId('run-status').props.children as string
  const field = text.split(';').find(item => item.startsWith(`${key}=`))
  return field === undefined ? null : field.slice(key.length + 1)
}

/** A live link: the inventory is only meaningful next to one. */
async function linked() {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
  const connection = { beginDisconnect: jest.fn(), beginDiscoverGatt: jest.fn() } as unknown as BlengConnection
  current.beginConnect.mockReturnValue({
    connect: {
      wait: jest.fn().mockResolvedValue({ connection } as unknown as BlengConnectResult),
      cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengConnect,
  })
  await press('connect-peripheral')
  expect(fact('link')).toBe('connected')
}

const outcome = (keys: string[]): BlengDeviceQueryOutcome => ({
  devices: keys.map(key => ({ key, localName: 'peer', connected: true, bonded: false })),
})

it('reports the inventory and the peer it says is connected', async () => {
  const wait = jest.fn<Promise<BlengDeviceQueryOutcome>, [number]>()
  current.beginDeviceQuery.mockReturnValue({ query: { wait, cancel: jest.fn() } } as unknown as BlengDeviceQuerySubmission)
  await linked()
  expect(fact('devices')).toBe('none')
  wait.mockResolvedValue(outcome([PEER_KEY]))
  await press('devices-query')
  expect(fact('devices')).toBe('done')
  expect(fact('devicesCount')).toBe('1')
  expect(fact('devicesPeer')).toBe('yes')
  // The declared service filter is the inventory request, and it was sent as the one that
  // is actually a filter: an empty request would let a nearby peer answer for this one.
  expect(current.beginDeviceQuery).toHaveBeenCalledWith('connected', expect.any(Array))
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/devices-query: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/devices-query-wait: succeeded/)
})

it('reports a list that omits the linked peer as a fact, not as success', async () => {
  const wait = jest.fn<Promise<BlengDeviceQueryOutcome>, [number]>()
  current.beginDeviceQuery.mockReturnValue({ query: { wait, cancel: jest.fn() } } as unknown as BlengDeviceQuerySubmission)
  await linked()
  // A list came back, but it is not this link: the scene must be able to say so.
  wait.mockResolvedValue(outcome(['some-other-peer']))
  await press('devices-query')
  expect(fact('devices')).toBe('done')
  expect(fact('devicesCount')).toBe('1')
  expect(fact('devicesPeer')).toBe('no')
})

it('keeps a platform that has no such inventory out of the success path', async () => {
  // A refused submission is synchronous; `devices` states the platform's own refusal.
  current.beginDeviceQuery.mockReturnValue({ failure: unsupported } as unknown as BlengDeviceQuerySubmission)
  await linked()
  await press('devices-query')
  expect(fact('devices')).toBe('unsupported')
  expect(fact('devicesPeer')).toBe('none')
})

it('answers a bonded inventory from the Probe page without any link', async () => {
  const wait = jest.fn<Promise<BlengDeviceQueryOutcome>, [number]>()
  current.beginDeviceQuery.mockReturnValue(
    { query: { wait, cancel: jest.fn() } } as unknown as BlengDeviceQuerySubmission,
  )
  render(<App />)
  await press('start-session')
  await press('workbench-probe')
  expect(fact('link')).toBe('none')
  wait.mockResolvedValue(outcome([]))
  await press('devices-bonded')
  expect(current.beginDeviceQuery).toHaveBeenCalledWith('bonded', expect.any(Array))
  expect(fact('devices')).toBe('done')
  expect(fact('devicesKind')).toBe('bonded')
  expect(fact('devicesCount')).toBe('0')
  // An adapter inventory has no peer to compare against.
  expect(fact('devicesPeer')).toBe('none')
})

it('offers no inventory query before there is a link to check it against', async () => {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  expect(screen.getByTestId('devices-query')).toBeDisabled()
})
