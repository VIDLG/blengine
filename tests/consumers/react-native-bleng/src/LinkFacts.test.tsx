import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengCharacteristicProperties, BlengReceipt, BlengConnect,
  BlengConnectOutcome, BlengConnectResult, BlengConnection, BlengFailureInfo,
  BlengGattCharacteristic, BlengGattDiscovery, BlengGattDiscoveryResult, BlengGattService,
  BlengGattSession, BlengMtuOutcome, BlengPeripheral,
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
/** The one code a platform uses for "this OS has no such capability". */
const unsupported: BlengFailureInfo = {
  domain: 'submission', code: 'bleng.dispatch_rejected.not_supported',
  message: 'CoreBluetooth does not support explicit MTU requests',
}
const deadline: BlengFailureInfo = {
  domain: 'observation', code: 'bleng.timeout', message: 'BLeng operation timed out',
}
const TARGET = '00002a37-0000-1000-8000-00805f9b34fb'

function properties(flags: Partial<BlengCharacteristicProperties>): BlengCharacteristicProperties {
  return {
    readable: false, writableWithResponse: false, writableWithoutResponse: false,
    notifiable: false, indicatable: false, ...flags,
  }
}

function fixture() {
  const beginDiscoverGatt = jest.fn()
  const discoveryWait = jest.fn<Promise<BlengGattDiscoveryResult>, [number]>()
  const beginConnect = jest.fn()
  const waitUntilStarted = jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({})
  const peripheral = { beginConnect } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted,
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'facts-target', advertisedServices: [], manufacturerData: [],
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
  return { central, scan, beginConnect, beginDiscoverGatt, discoveryWait }
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

/** Links and discovers, so the link-fact strip is addressable. */
async function linked(linkFacts: Record<string, unknown>) {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
  const connection = {
    beginDisconnect: jest.fn(), beginDiscoverGatt: current.beginDiscoverGatt,
    beginReadRssi: jest.fn(), ...linkFacts,
  } as unknown as BlengConnection
  current.beginConnect.mockReturnValue({
    connect: {
      wait: jest.fn().mockResolvedValue({ connection } as BlengConnectResult),
      cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengConnect,
  })
  await press('connect-peripheral')
  const service = {
    uuid: () => '0000180d-0000-1000-8000-00805f9b34fb',
    kind: () => 'primary',
    characteristics: () => [{
      uuid: () => TARGET, properties: () => properties({ notifiable: true }), descriptors: () => [],
      beginRead: jest.fn(), beginWrite: jest.fn(), beginSubscription: jest.fn(),
    } as unknown as BlengGattCharacteristic],
  } as unknown as BlengGattService
  const session = { services: () => [service] } as unknown as BlengGattSession
  current.beginDiscoverGatt.mockReturnValue({
    discovery: {
      wait: current.discoveryWait, cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengGattDiscovery,
  })
  current.discoveryWait.mockResolvedValue({ session })
  await press('discover-gatt')
  fireEvent.changeText(screen.getByTestId('gatt-target'), TARGET)
}

const acceptedMtu = (info: object): BlengMtuOutcome => ({ info } as BlengMtuOutcome)

it('reports the queried MTU as the platform declares it, without inventing one', async () => {
  const wait = jest.fn<Promise<BlengMtuOutcome>, [number]>()
  await linked({
    beginQueryMtu: () => ({ mtu: { wait, cancel: jest.fn() } }),
  })
  wait.mockResolvedValue(acceptedMtu({ mtu: 247, maximumWriteWithResponse: 244, maximumWriteWithoutResponse: 241 }))
  expect(fact('mtu')).toBe('none')
  await press('mtu-query')
  expect(fact('mtu')).toBe('done')
  // The write limits are what both platforms report; the negotiated MTU is not repeated
  // here because CoreBluetooth never has one.
  expect(fact('mtuWrites')).toBe('244/241')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/mtu-query: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/mtu-query-wait: succeeded/)
})

it('reports a platform refusal as unsupported instead of as a failure', async () => {
  // CoreBluetooth cannot request an MTU. The App must say so, and a scene must be able to
  // tell that answer apart from a command that simply failed.
  const wait = jest.fn<Promise<{ failure: BlengFailureInfo }>, [number]>()
  await linked({
    beginRequestMtu: () => ({ mtu: { wait, cancel: jest.fn() } }),
  })
  wait.mockResolvedValue({ failure: unsupported })
  await press('mtu-request')
  expect(fact('mtuRequest')).toBe('unsupported')
  expect(fact('failure')).toBe('submission/bleng.dispatch_rejected.not_supported')
})

it('reports a request that really failed as failed, not as unsupported', async () => {
  const wait = jest.fn<Promise<{ failure: BlengFailureInfo }>, [number]>()
  await linked({
    beginRequestMtu: () => ({ mtu: { wait, cancel: jest.fn() } }),
  })
  wait.mockResolvedValue({ failure: deadline })
  await press('mtu-request')
  expect(fact('mtuRequest')).toBe('failed')
})

it('keeps the priority and refresh terminals apart from each other', async () => {
  const priorityWait = jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({})
  const refreshWait = jest.fn<Promise<BlengReceipt>, [number]>()
    .mockResolvedValue({ failure: unsupported })
  await linked({
    beginSetConnectionPriority: () => ({ priority: { wait: priorityWait, cancel: jest.fn() } }),
    beginGattCacheRefresh: () => ({ refresh: { wait: refreshWait, cancel: jest.fn() } }),
  })
  await press('priority-high')
  expect(fact('priority')).toBe('done')
  await press('refresh-gatt')
  // Android can invalidate its cache; CoreBluetooth cannot. Neither may leak into the other.
  expect(fact('refresh')).toBe('unsupported')
  expect(fact('priority')).toBe('done')
})

it('offers no link fact before a link exists', async () => {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  for (const identifier of ['mtu-query', 'mtu-request', 'priority-high', 'refresh-gatt']) {
    expect(screen.getByTestId(identifier)).toBeDisabled()
  }
})

it('keeps an accepted submission visible when its wait never settles', async () => {
  // A submission is not a fact about the link: the phase stays `pending`, and the ledger says
  // the submission was accepted while its wait is still outstanding.
  const wait = jest.fn<Promise<BlengMtuOutcome>, [number]>(() => new Promise(() => {}))
  await linked({
    beginQueryMtu: () => ({ mtu: { wait, cancel: jest.fn() } }),
  })
  await act(async () => { fireEvent.press(screen.getByTestId('mtu-query')) })
  expect(fact('mtu')).toBe('pending')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/mtu-query: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/mtu-query-wait: pending/)
})

/** Unused in assertions; keeps the import of the connect type honest for type-checking. */
export type AcceptedConnect = BlengConnectOutcome
