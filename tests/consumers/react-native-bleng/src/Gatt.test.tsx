import React from 'react'
import { act, fireEvent, render, screen, within } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengReceipt, BlengConnect, BlengConnectResult, BlengConnection,
  BlengFailureInfo, BlengGattCharacteristic, BlengGattDiscovery, BlengGattDiscoveryResult,
  BlengGattService, BlengGattSession, BlengPeripheral, BlengRead, BlengReadResult, BlengRssi,
  BlengRssiResult, BlengScan, BlengScanAdvertisement, BlengScanNext, BlengCentral, BlengWrite,
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
  domain: 'operation', code: 'bleng.not_ready.gatt_not_ready', message: 'GATT is not ready',
}

function characteristic(
  uuid: string,
  flags: { readable?: boolean; writableWithResponse?: boolean; writableWithoutResponse?: boolean },
  capability: BlengGattCharacteristic,
): BlengGattCharacteristic {
  const properties = {
    readable: flags.readable ?? false,
    writableWithResponse: flags.writableWithResponse ?? false,
    writableWithoutResponse: flags.writableWithoutResponse ?? false,
    notifiable: false,
    indicatable: false,
  }
  return {
    uuid: () => uuid,
    properties: () => properties,
    descriptors: () => [{ uuid: () => '00002902-0000-1000-8000-00805f9b34fb' }],
    beginRead: capability.beginRead,
    beginWrite: capability.beginWrite,
  } as unknown as BlengGattCharacteristic
}

function fixture() {
  const readWait = jest.fn<Promise<BlengReadResult>, [number]>()
  const readCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})
  const writeWait = jest.fn<Promise<BlengReceipt>, [number]>()
  const writeCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})
  const beginRead = jest.fn()
  const beginWrite = jest.fn()
  const beginDiscoverGatt = jest.fn()
  const beginReadRssi = jest.fn()
  const discoveryWait = jest.fn<Promise<BlengGattDiscoveryResult>, [number]>()
  const discoveryCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})
  const rssiWait = jest.fn<Promise<BlengRssiResult>, [number]>()
  const rssiCancel = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})

  const peripheral = {
    beginConnect: jest.fn(),
  } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'gatt-target', advertisedServices: [], manufacturerData: [],
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
    central, scan, peripheral, beginRead, beginWrite, readWait, readCancel, writeWait, writeCancel,
    beginDiscoverGatt, beginReadRssi, discoveryWait, discoveryCancel, rssiWait, rssiCancel,
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

/** The action strip acts on a characteristic UUID the caller declares, never a row key. */
function declare(uuid: string) {
  fireEvent.changeText(screen.getByTestId('gatt-target'), uuid)
}

const READABLE = '00002a37-0000-1000-8000-00805f9b34fb'
const WRITE_ONLY = '00002a39-0000-1000-8000-00805f9b34fb'

/** Reaches a live connection through the real UI path. */
async function connected() {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
  const connection = { beginDisconnect: jest.fn(), beginDiscoverGatt: current.beginDiscoverGatt,
    beginReadRssi: current.beginReadRssi } as unknown as BlengConnection
  current.peripheral.beginConnect = jest.fn().mockReturnValue({
    connect: {
      wait: jest.fn().mockResolvedValue({ connection } as BlengConnectResult),
      cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengConnect,
  })
  await press('connect-peripheral')
}

/** Discovers a two-characteristic service: one readable+writable, one write-only. */
function acceptDiscovery(services?: BlengGattService[]) {
  const read = { wait: current.readWait, cancel: current.readCancel } as unknown as BlengRead
  const write = { wait: current.writeWait, cancel: current.writeCancel } as unknown as BlengWrite
  current.beginRead.mockReturnValue({ read })
  current.beginWrite.mockReturnValue({ write })
  current.readWait.mockResolvedValue({ value: new Uint8Array([0x10, 0x11]).buffer })
  current.writeWait.mockResolvedValue({})

  const readable = characteristic(
    '00002a37-0000-1000-8000-00805f9b34fb',
    { readable: true, writableWithResponse: true },
    { beginRead: current.beginRead, beginWrite: current.beginWrite } as unknown as BlengGattCharacteristic,
  )
  const writeOnly = characteristic(
    '00002a39-0000-1000-8000-00805f9b34fb',
    { writableWithoutResponse: true },
    { beginRead: current.beginRead, beginWrite: current.beginWrite } as unknown as BlengGattCharacteristic,
  )
  const service = {
    uuid: () => '0000180d-0000-1000-8000-00805f9b34fb',
    kind: () => 'primary',
    characteristics: () => [readable, writeOnly],
  } as unknown as BlengGattService
  acceptTopology(services ?? [service])
}

/** Discovers exactly these services; the discovery capability itself always succeeds. */
function acceptTopology(services: BlengGattService[]) {
  const session = { services: () => services } as unknown as BlengGattSession
  const discovery = {
    wait: current.discoveryWait, cancel: current.discoveryCancel,
  } as unknown as BlengGattDiscovery
  current.beginDiscoverGatt.mockReturnValue({ discovery })
  current.discoveryWait.mockResolvedValue({ session })
}

/** A primary service holding one read-only characteristic per supplied UUID. */
function service(uuid: string, characteristicUuids: string[]): BlengGattService {
  return {
    uuid: () => uuid,
    kind: () => 'primary',
    characteristics: () => characteristicUuids.map(entry => characteristic(
      entry, { readable: true }, {} as unknown as BlengGattCharacteristic,
    )),
  } as unknown as BlengGattService
}

it('discovers through the live connection and projects a bounded topology', async () => {
  acceptDiscovery()
  await connected()
  expect(screen.getByTestId('gatt-state')).toHaveTextContent(/^none$/)
  expect(screen.getByTestId('discover-gatt')).toBeEnabled()
  expect(current.beginDiscoverGatt).not.toHaveBeenCalled()

  await press('discover-gatt')
  expect(current.beginDiscoverGatt).toHaveBeenCalledTimes(1)
  expect(current.discoveryWait).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('gatt-state')).toHaveTextContent(/^ready$/)
  expect(screen.getByTestId('gatt-summary')).toHaveTextContent(/1 services, 2 characteristics/)
  expect(screen.getByTestId('gatt-summary')).toHaveTextContent(/0 not addressable/)
  // The addressable index states exactly what the host may key against, including the
  // platform-reported properties it must not re-derive.
  expect(screen.getByTestId('gatt-rows')).toHaveTextContent(
    /0\.0 00002a37-0000-1000-8000-00805f9b34fb \[read,write\][\s\S]*0\.1 00002a39-0000-1000-8000-00805f9b34fb \[writeNoResponse\]/,
  )

  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/gatt-discover: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/gatt-discover-wait: succeeded/)
})

it('addresses characteristics from every discovered service, not only the first ones', async () => {
  // A real transport lists its standard services before the application's own service, so a
  // discovery-order budget would spend itself on those and leave the target unreachable.
  acceptDiscovery([
    service('00001800-0000-1000-8000-00805f9b34fb', ['00002a00-0000-1000-8000-00805f9b34fb']),
    service('00001801-0000-1000-8000-00805f9b34fb', [
      '00002a05-0000-1000-8000-00805f9b34fb',
      '00002a06-0000-1000-8000-00805f9b34fb',
      '00002a07-0000-1000-8000-00805f9b34fb',
      '00002a08-0000-1000-8000-00805f9b34fb',
    ]),
    service('0000180a-0000-1000-8000-00805f9b34fb', [
      '00002a29-0000-1000-8000-00805f9b34fb',
      '00002a24-0000-1000-8000-00805f9b34fb',
      '00002a25-0000-1000-8000-00805f9b34fb',
      '00002a27-0000-1000-8000-00805f9b34fb',
    ]),
    service('fd600000-0000-1000-8000-00805f9b34fb', [
      'fff20000-0000-1000-8000-00805f9b34fb',
      'fff50000-0000-1000-8000-00805f9b34fb',
    ]),
  ])
  await connected()
  await press('discover-gatt')
  expect(screen.getByTestId('gatt-summary')).toHaveTextContent(/4 services, 11 characteristics/)
  expect(screen.getByTestId('gatt-summary')).toHaveTextContent(/0 not addressable/)
  expect(screen.getByTestId('gatt-rows')).toHaveTextContent(
    /0\.0 00002a00-[\s\S]*3\.0 fff20000-[\s\S]*3\.1 fff50000-/,
  )
})

it('reads only from a readable characteristic and shows the raw bytes', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(WRITE_ONLY)
  expect(screen.getByTestId('gatt-read')).toBeDisabled()
  expect(screen.getByTestId('gatt-target-status')).toHaveTextContent(/\[writeNoResponse\]/)
  declare(READABLE)
  expect(screen.getByTestId('gatt-target-status')).toHaveTextContent(/\[read,write\]/)
  await press('gatt-read')
  expect(current.beginRead).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('gatt-result')).toHaveTextContent(/00002a37.*2 bytes \[10 11\]/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/gatt-read-wait: succeeded/)
})

/**
 * A read that is genuinely outstanding.
 *
 * The rest of this suite resolves every wait immediately, which is why cancellation had no
 * test: there was never a window in which a wait existed. This holds one open.
 */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(settle => { resolve = settle })
  return { promise, resolve }
}

// The exact vocabulary the supervisor projects for `BlengError::Cancelled`.
const CANCELLED = {
  domain: 'operation', code: 'bleng.cancelled', message: 'BLeng operation was cancelled',
} as unknown as BlengFailureInfo

it('cancels an outstanding read, reports the cancelled terminal, and reads again', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(READABLE)
  const pending = deferred<BlengReadResult>()
  current.readWait.mockReturnValueOnce(pending.promise)
  // The read is submitted and its wait is outstanding: no value, no failure yet.
  await press('gatt-read')
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=waiting(;|$)/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattReadTotal=0(;|$)/)
  expect(screen.getByTestId('cancel-gatt-read')).toBeEnabled()
  await press('cancel-gatt-read')
  expect(current.readCancel).toHaveBeenCalledTimes(1)
  await act(async () => { pending.resolve({ failure: CANCELLED }) })
  // A cancellation the operator asked for is a terminal, not a peer fault.
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=cancelled(;|$)/)
  // A settled count is what tells two sequential reads apart: the queue is empty again and
  // `gattRead` returns to `done`, but this run has now settled exactly one wait.
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattReads=0(;|$)/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattReadTotal=1(;|$)/)
  expect(screen.getByTestId('gatt-cancel')).toHaveTextContent(/cancelled/)
  expect(screen.queryByTestId('gatt-result')).toBeNull()
  expect(screen.queryByTestId('gatt-error')).toBeNull()
  // The cancelled read is not the last word: the same characteristic reads normally after.
  current.readWait.mockResolvedValue({ value: new Uint8Array([0x10, 0x11]).buffer })
  await press('gatt-read')
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=done(;|$)/)
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattReadTotal=2(;|$)/)
  expect(screen.getByTestId('gatt-result')).toHaveTextContent(/2 bytes \[10 11\]/)
  expect(screen.queryByTestId('gatt-cancel')).toBeNull()
})

it('cancels the queued read while the active one still completes', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(READABLE)
  const active = deferred<BlengReadResult>()
  const queued = deferred<BlengReadResult>()
  const activeCancel = jest.fn().mockResolvedValue({})
  const queuedCancel = jest.fn().mockResolvedValue({})
  // Two submissions: the native lane is a FIFO, so the second one is queued behind the first.
  current.beginRead
    .mockReturnValueOnce({ read: { wait: () => active.promise, cancel: activeCancel } })
    .mockReturnValueOnce({ read: { wait: () => queued.promise, cancel: queuedCancel } })
  await press('gatt-read')
  await press('gatt-read')
  await press('cancel-gatt-read')
  // The newest outstanding read is the cancellable one; the active read is left alone.
  expect(queuedCancel).toHaveBeenCalledTimes(1)
  expect(activeCancel).not.toHaveBeenCalled()
  await act(async () => { queued.resolve({ failure: CANCELLED }) })
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=cancelled(;|$)/)
  // Both submissions settled as accepted; only the queued *wait* failed.
  await press('workbench-evidence')
  const evidence = String(screen.getByTestId('lifecycle-evidence').props.children)
  expect(evidence.match(/gatt-read: succeeded/g)).toHaveLength(2)
  expect(evidence.match(/gatt-read-wait: failed/g)).toHaveLength(1)
  expect(evidence.match(/gatt-read: pending/g)).toBeNull()
  await press('workbench-current')
  await act(async () => { active.resolve({ value: new Uint8Array([0x2a, 0x2b]).buffer }) })
  // The active read was never cancelled: it completes normally and is what the UI shows.
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=done(;|$)/)
  expect(screen.getByTestId('gatt-result')).toHaveTextContent(/2 bytes \[2a 2b\]/)
})

it('reports a cancellation this run did not request as a failure', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(READABLE)
  current.readWait.mockResolvedValueOnce({ failure: CANCELLED })
  await press('gatt-read')
  // Nothing was cancelled from the UI, so the same native code is an unexplained failure.
  expect(screen.getByTestId('run-status')).toHaveTextContent(/gattRead=failed(;|$)/)
  expect(screen.getByTestId('gatt-error')).toHaveTextContent(/cancelled/)
  expect(screen.queryByTestId('gatt-cancel')).toBeNull()
})

it('offers no read cancellation when no read is outstanding', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(READABLE)
  expect(screen.getByTestId('cancel-gatt-read')).toBeDisabled()
  await press('gatt-read')
  expect(screen.getByTestId('cancel-gatt-read')).toBeDisabled()
  expect(current.readCancel).not.toHaveBeenCalled()
})

it('pins every outcome a scene reads outside the scrolled content', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare(READABLE)
  await press('gatt-read')
  current.beginReadRssi.mockReturnValue({
    rssi: { wait: current.rssiWait, cancel: current.rssiCancel } as unknown as BlengRssi,
  })
  current.rssiWait.mockResolvedValue({ rssi: -47 })
  await press('read-rssi')
  const console_ = within(screen.getByTestId('command-console'))
  const content = within(screen.getByTestId('workbench-content'))
  // The value that followed the command, and the command that produced it, are reachable
  // together: a scene reads what it just caused without a gesture.
  expect(console_.getByTestId('gatt-read')).toBeTruthy()
  expect(console_.getByTestId('gatt-result')).toHaveTextContent(/2 bytes \[10 11\]/)
  expect(console_.getByTestId('rssi-value')).toHaveTextContent(/^-47 dBm$/)
  expect(content.queryByTestId('gatt-result')).toBeNull()
  expect(content.queryByTestId('rssi-value')).toBeNull()
})

it('acts on no characteristic when the declared UUID names none of them', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  declare('00009999-0000-1000-8000-00805f9b34fb')
  expect(screen.getByTestId('gatt-target-status')).toHaveTextContent(/No discovered characteristic matches/)
  expect(screen.getByTestId('gatt-read')).toBeDisabled()
  expect(screen.getByTestId('gatt-write')).toBeDisabled()
  expect(screen.getByTestId('gatt-subscribe')).toBeDisabled()
  await press('gatt-read')
  expect(current.beginRead).not.toHaveBeenCalled()
})

it('uses the declared write mode and never claims a read-back', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  fireEvent.changeText(screen.getByTestId('gatt-write-value'), 'AB')
  declare(WRITE_ONLY)
  await press('gatt-write')
  expect(current.beginWrite).toHaveBeenCalledTimes(1)
  const [payload, writeType] = current.beginWrite.mock.calls[0]
  expect(writeType).toBe('withoutResponse')
  expect(Array.from(new Uint8Array(payload as ArrayBuffer))).toEqual([0x41, 0x42])
  expect(screen.getByTestId('gatt-result')).toHaveTextContent(/wrote 2 bytes \(withoutResponse\)/)
  // The accepted submission settles its own receipt: the wait that follows is a
  // separate fact, so a write that was accepted cannot read as a never-answered one.
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/gatt-write: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/gatt-write-wait: succeeded/)
  await press('workbench-current')
  declare(READABLE)
  await press('gatt-write')
  expect(current.beginWrite.mock.calls[1][1]).toBe('withResponse')
})

it('keeps a refused discovery visible without claiming a topology', async () => {
  await connected()
  current.beginDiscoverGatt.mockReturnValue({ failure: refused })
  await press('discover-gatt')
  expect(current.discoveryWait).not.toHaveBeenCalled()
  expect(screen.getByTestId('gatt-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('gatt-error')).toHaveTextContent(/bleng.not_ready.gatt_not_ready/)
  expect(screen.getByTestId('gatt-summary')).toHaveTextContent(/No topology/)
  expect(screen.getByTestId('gatt-read')).toBeDisabled()
  expect(screen.getByTestId('gatt-rows')).toHaveTextContent(/^none$/)
})

it('distinguishes a zero and a negative RSSI reading from no reading', async () => {
  await connected()
  current.beginReadRssi.mockReturnValue({
    rssi: { wait: current.rssiWait, cancel: current.rssiCancel } as unknown as BlengRssi,
  })
  current.rssiWait.mockResolvedValue({ rssi: 0 })
  await press('read-rssi')
  expect(screen.getByTestId('rssi-value')).toHaveTextContent(/^0 dBm$/)
  current.beginReadRssi.mockReturnValue({
    rssi: { wait: current.rssiWait, cancel: current.rssiCancel } as unknown as BlengRssi,
  })
  current.rssiWait.mockResolvedValue({ rssi: -71 })
  await press('read-rssi')
  expect(screen.getByTestId('rssi-value')).toHaveTextContent(/^-71 dBm$/)
})

it('withdraws the capability controls when the engine is closing', async () => {
  acceptDiscovery()
  await connected()
  await press('discover-gatt')
  await press('close-session')
  expect(screen.getByTestId('discover-gatt')).toBeDisabled()
  expect(screen.getByTestId('read-rssi')).toBeDisabled()
  expect(screen.getByTestId('gatt-read')).toBeDisabled()
  expect(screen.getByTestId('gatt-write')).toBeDisabled()
  expect(screen.getByTestId('gatt-subscribe')).toBeDisabled()
})
