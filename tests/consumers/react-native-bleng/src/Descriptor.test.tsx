import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blehub/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengCharacteristicProperties, BlengReceipt, BlengConnect,
  BlengConnectResult, BlengConnection, BlengDescriptorRead, BlengDescriptorWrite,
  BlengFailureInfo,
  BlengGattCharacteristic, BlengGattDescriptor, BlengGattDiscovery, BlengGattDiscoveryResult,
  BlengGattService, BlengGattSession, BlengPeripheral, BlengScan, BlengScanAdvertisement,
  BlengScanNext, BlengCentral,
} from '@blehub/react-native-bleng'
import App from './App'

jest.mock('@blehub/react-native-bleng', () => ({
  ...jest.requireActual('@blehub/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))
const createCentral = jest.mocked(createBlengCentral)
afterEach(() => { closeTrace.mockRestore() })

const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized',
}
const deadline: BlengFailureInfo = {
  domain: 'observation', code: 'bleng.timeout', message: 'BLeng operation timed out',
}
const TARGET = '00002a37-0000-1000-8000-00805f9b34fb'
const CCCD = '00002902-0000-1000-8000-00805f9b34fb'

function properties(flags: Partial<BlengCharacteristicProperties>): BlengCharacteristicProperties {
  return {
    readable: false, writableWithResponse: false, writableWithoutResponse: false,
    notifiable: false, indicatable: false, ...flags,
  }
}

function descriptorFixture() {
  const readWait = jest.fn<Promise<{ value?: ArrayBuffer }>, [number]>()
  const writeWait = jest.fn<Promise<BlengReceipt>, [number]>()
  const beginWrite = jest.fn()
  const descriptor = {
    uuid: () => CCCD,
    beginRead: () => ({ read: { wait: readWait, cancel: jest.fn() } } as unknown as BlengDescriptorRead),
    beginWrite: (value: ArrayBuffer) => {
      // A platform may refuse the submission before anything is queued (CoreBluetooth refuses
      // a direct CCCD write), so the fixture lets a test return that refusal itself.
      return beginWrite(value)
        ?? ({ write: { wait: writeWait, cancel: jest.fn() } } as unknown as BlengDescriptorWrite)
    },
  } as unknown as BlengGattDescriptor
  return { descriptor, readWait, writeWait, beginWrite }
}

function fixture() {
  const beginDiscoverGatt = jest.fn()
  const discoveryWait = jest.fn<Promise<BlengGattDiscoveryResult>, [number]>()
  const beginConnect = jest.fn()
  const peripheral = { beginConnect } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'cccd-target', advertisedServices: [], manufacturerData: [],
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
  return { central, beginConnect, beginDiscoverGatt, discoveryWait }
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

/** Links, discovers and declares the target; `descriptors` is what the peer exposes. */
async function linked(descriptors: BlengGattDescriptor[]) {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
  const connection = { beginDisconnect: jest.fn(), beginDiscoverGatt: current.beginDiscoverGatt,
    beginReadRssi: jest.fn() } as unknown as BlengConnection
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
      uuid: () => TARGET, properties: () => properties({ notifiable: true }),
      descriptors: () => descriptors,
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

it('reads the peer\u2019s own CCCD value as a separate fact', async () => {
  const cccd = descriptorFixture()
  await linked([cccd.descriptor])
  expect(fact('descriptor')).toBe('none')
  cccd.readWait.mockResolvedValue({ value: new Uint8Array([0x00, 0x00]).buffer })
  await press('descriptor-read')
  expect(fact('descriptor')).toBe('done')
  expect(fact('descriptorValue')).toBe('0000')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/descriptor-read: succeeded/)
})

it('counts settled reads, so a same-value re-read is still a new fact', async () => {
  // The scene's `advance` guard depends on this: the App keeps showing the previous value, so
  // only the counter can prove that a second read of the *same* bytes actually happened.
  const cccd = descriptorFixture()
  await linked([cccd.descriptor])
  expect(fact('descriptorReadTotal')).toBe('0')
  cccd.readWait.mockResolvedValue({ value: new Uint8Array([0x00, 0x00]).buffer })
  await press('descriptor-read')
  expect(fact('descriptorValue')).toBe('0000')
  expect(fact('descriptorReadTotal')).toBe('1')
  await press('descriptor-read')
  // Same bytes, a genuinely newer read.
  expect(fact('descriptorValue')).toBe('0000')
  expect(fact('descriptorReadTotal')).toBe('2')
})

it('says the descriptor is absent instead of inventing a read', async () => {
  // A characteristic with no CCCD cannot carry a subscription; that is a topology fact.
  await linked([])
  await press('descriptor-read')
  expect(fact('descriptor')).toBe('absent')
  expect(fact('descriptorValue')).toBe('none')
  expect(screen.getByTestId('descriptor-write')).toBeDisabled()
})

it('writes back exactly the bytes the peer reported, and only after a read', async () => {
  const cccd = descriptorFixture()
  await linked([cccd.descriptor])
  // Nothing was read yet, so there is nothing honest to write.
  expect(screen.getByTestId('descriptor-write')).toBeDisabled()
  cccd.readWait.mockResolvedValue({ value: new Uint8Array([0x01, 0x00]).buffer })
  await press('descriptor-read')
  expect(fact('descriptorValue')).toBe('0100')
  cccd.writeWait.mockResolvedValue({})
  await press('descriptor-write')
  expect(fact('descriptorWrite')).toBe('done')
  expect(cccd.beginWrite).toHaveBeenCalledTimes(1)
  expect(Array.from(new Uint8Array(cccd.beginWrite.mock.calls[0][0] as ArrayBuffer)))
    .toEqual([0x01, 0x00])
})

it('reports a refused descriptor read as failed, not as absent', async () => {
  const cccd = descriptorFixture()
  await linked([cccd.descriptor])
  cccd.readWait.mockRejectedValue(new Error('BLeng operation timed out'))
  await press('descriptor-read')
  expect(fact('descriptor')).toBe('failed')
  // A failed read still settled: the counter advances so a scene is not stuck waiting for it.
  expect(fact('descriptorReadTotal')).toBe('1')
  expect(fact('failure')).not.toBe('none')
})

it('reports a platform that refuses the write as itself, not as a failed write', async () => {
  // CoreBluetooth refuses a direct CCCD write (it demands setNotifyValue), so the package
  // answers with its typed not_supported code. That is a platform answer: the App must say
  // `unsupported` rather than blur it into `failed`, and the bytes it read stay on screen.
  const cccd = descriptorFixture()
  await linked([cccd.descriptor])
  cccd.readWait.mockResolvedValue({ value: new Uint8Array([0x00, 0x00]).buffer })
  await press('descriptor-read')
  expect(fact('descriptorValue')).toBe('0000')
  cccd.beginWrite.mockReturnValue({
    failure: {
      domain: 'submission',
      code: 'bleng.dispatch_rejected.not_supported',
      message: 'CoreBluetooth configures a CCCD through setNotifyValue(_:for:)',
    } as BlengFailureInfo,
  })
  await press('descriptor-write')
  expect(fact('descriptorWrite')).toBe('unsupported')
  expect(fact('failure')).toBe('submission/bleng.dispatch_rejected.not_supported')
  // The peer's value is unchanged because nothing was written.
  expect(fact('descriptorValue')).toBe('0000')
})
