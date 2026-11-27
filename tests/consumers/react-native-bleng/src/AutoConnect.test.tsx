import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengConnectResult, BlengConnection, BlengFailureInfo,
  BlengPeripheral, BlengScan, BlengScanAdvertisement, BlengScanNext, BlengCentral,
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
const unsupported: BlengFailureInfo = {
  domain: 'submission', code: 'bleng.dispatch_rejected.not_supported',
  message: 'CoreBluetooth cannot request an auto-connect link',
}

function fixture() {
  const beginAutoConnect = jest.fn()
  const beginConnect = jest.fn()
  const peripheral = { beginAutoConnect, beginConnect, key: () => 'peer-1' } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<object>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<object>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'auto-target', advertisedServices: [], manufacturerData: [],
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
  return { central, scan, beginAutoConnect }
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

/** Scans so a peripheral capability exists, then opens the Probe page. */
async function probed() {
  render(<App />)
  await press('start-session')
  await press('start-scan')
  await press('next-scan-event')
  await press('workbench-probe')
  expect(screen.getByTestId('workbench-page')).toHaveTextContent(/^Probe$/)
}

const connection = (): BlengConnection =>
  ({ beginDisconnect: jest.fn(), beginDiscoverGatt: jest.fn() }) as unknown as BlengConnection

it('runs a standing auto-connect from the Probe page, not the observation console', async () => {
  const wait = jest.fn<Promise<BlengConnectResult>, [number]>()
  current.beginAutoConnect.mockReturnValue({ connect: { wait, cancel: jest.fn() } })
  await probed()
  // The probe surface is pinned on its own page; the observation console is not rendered here.
  expect(screen.getByTestId('probe-console')).toBeVisible()
  expect(screen.queryByTestId('command-console')).toBeNull()
  expect(fact('auto')).toBe('none')
  wait.mockResolvedValue({ connection: connection() } as unknown as BlengConnectResult)
  await press('auto-connect')
  expect(fact('auto')).toBe('done')
  expect(fact('link')).toBe('connected')
  expect(current.beginAutoConnect).toHaveBeenCalledTimes(1)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/auto-connect: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/auto-connect-wait: succeeded/)
})

it("reports a platform's refusal as unsupported, never as a failure or a timeout", async () => {
  current.beginAutoConnect.mockReturnValue({ failure: unsupported })
  await probed()
  await press('auto-connect')
  expect(fact('auto')).toBe('unsupported')
  expect(fact('link')).toBe('none')
  expect(fact('failure')).toBe('submission/bleng.dispatch_rejected.not_supported')
})

it('offers no standing request before the App has observed a peer to address', async () => {
  render(<App />)
  await press('start-session')
  await press('workbench-probe')
  // No scan has produced a peripheral capability, so there is nothing to ask the platform
  // about; enabling the control here would dispatch a request with no peer identity.
  expect(screen.getByTestId('auto-connect')).toBeDisabled()
})
