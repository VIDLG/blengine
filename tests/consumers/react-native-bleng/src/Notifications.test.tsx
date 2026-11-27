import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blengine/react-native-bleng'
import type {
  BlengAdapterSnapshot, BlengCharacteristicProperties, BlengReceipt, BlengConnect,
  BlengConnectResult, BlengConnection, BlengFailureInfo, BlengGattCharacteristic,
  BlengGattDiscovery, BlengGattDiscoveryResult, BlengGattService, BlengGattSession,
  BlengSubscriptionNext, BlengSubscriptionResult, BlengSubscription, BlengPeripheral,
  BlengScan, BlengScanAdvertisement, BlengScanNext, BlengCentral,
  BlengConnectionNext,
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
const deadline: BlengFailureInfo = {
  domain: 'observation', code: 'bleng.timeout', message: 'BLeng operation timed out',
}

function properties(flags: Partial<BlengCharacteristicProperties>): BlengCharacteristicProperties {
  return {
    readable: false, writableWithResponse: false, writableWithoutResponse: false,
    notifiable: false, indicatable: false, ...flags,
  }
}

/** One value a test resolves later; the App's end observer awaits it in the background. */
function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>(next => { resolve = next })
  return { promise, resolve }
}

function fixture() {
  const enableWait = jest.fn<Promise<BlengSubscriptionResult>, [number]>()
  const nextEvent = jest.fn<Promise<BlengSubscriptionNext>, [number]>()
  const disableWait = jest.fn<Promise<BlengReceipt>, [number]>()
  const beginSubscription = jest.fn()
  const beginDisable = jest.fn()
  const beginDiscoverGatt = jest.fn()
  const discoveryWait = jest.fn<Promise<BlengGattDiscoveryResult>, [number]>()
  const beginConnect = jest.fn()
  const linkEnd = deferred<BlengConnectionNext>()
  const peripheral = { beginConnect } as unknown as BlengPeripheral
  const scan = {
    waitUntilStarted: jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({}),
    cancelStart: jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({}),
    nextEvent: jest.fn<Promise<BlengScanNext>, [number]>().mockResolvedValue({
      advertisement: {
        peripheral, localName: 'notify-target', advertisedServices: [], manufacturerData: [],
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
    central, scan, peripheral, beginConnect, beginSubscription, beginDisable, enableWait,
    nextEvent, disableWait, beginDiscoverGatt, discoveryWait, linkEnd,
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

/** The action strip acts on the characteristic UUID the caller declares. */
function setKey(value: string) {
  fireEvent.changeText(screen.getByTestId('gatt-target'), value)
}

const NOTIFIABLE = '00002a37-0000-1000-8000-00805f9b34fb'
const READ_ONLY = '00002a39-0000-1000-8000-00805f9b34fb'

/** One notify+indicate characteristic and one read-only characteristic. */
function acceptDiscovery() {
  const notifyCapability = {
    beginRead: jest.fn(),
    beginWrite: jest.fn(),
    beginSubscription: current.beginSubscription,
  } as unknown as BlengGattCharacteristic
  const plainCapability = {
    beginRead: jest.fn(),
    beginWrite: jest.fn(),
    beginSubscription: jest.fn(),
  } as unknown as BlengGattCharacteristic
  const characteristic = (
    uuid: string, flags: Partial<BlengCharacteristicProperties>,
    capability: BlengGattCharacteristic,
  ) => ({
    uuid: () => uuid,
    properties: () => properties(flags),
    descriptors: () => [],
    beginRead: capability.beginRead,
    beginWrite: capability.beginWrite,
    beginSubscription: capability.beginSubscription,
  } as unknown as BlengGattCharacteristic)
  const service = {
    uuid: () => '0000180d-0000-1000-8000-00805f9b34fb',
    kind: () => 'primary',
    characteristics: () => [
      characteristic('00002a37-0000-1000-8000-00805f9b34fb', { notifiable: true }, notifyCapability),
      characteristic('00002a39-0000-1000-8000-00805f9b34fb', { readable: true }, plainCapability),
    ],
  } as unknown as BlengGattService
  const session = { services: () => [service] } as unknown as BlengGattSession
  current.beginDiscoverGatt.mockReturnValue({
    // `pendingOperation` accepts only an operation submission (wait + cancel); a lane-shaped
    // double is refused by name rather than awaited forever.
    discovery: {
      wait: current.discoveryWait,
      cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengGattDiscovery,
  })
  current.discoveryWait.mockResolvedValue({ session })
}

/** Reaches live notifications through the real UI path. */
async function subscribed(mode: 'notification' | 'indication' | 'unreported' = 'notification') {
  render(<App />)
  await press('start-session')
  await press('workbench-current')
  await press('start-scan')
  await press('next-scan-event')
  const connection = { beginDisconnect: jest.fn(), beginDiscoverGatt: current.beginDiscoverGatt,
    beginReadRssi: jest.fn(), awaitEnd: jest.fn().mockReturnValue(current.linkEnd.promise),
  } as unknown as BlengConnection
  current.beginConnect.mockReturnValue({
    connect: {
      wait: jest.fn().mockResolvedValue({ connection } as BlengConnectResult),
      cancel: jest.fn().mockResolvedValue({}),
    } as unknown as BlengConnect,
  })
  await press('connect-peripheral')
  acceptDiscovery()
  await press('discover-gatt')
  current.beginSubscription.mockReturnValue({
    notifications: {
      waitUntilEnabled: current.enableWait,
      nextEvent: current.nextEvent,
      beginDisable: current.beginDisable,
      detach: jest.fn(),
    } as unknown as BlengSubscription,
  })
  current.enableWait.mockResolvedValue(mode === 'unreported' ? {} : { mode })
  // Nothing acts until a characteristic is declared: the strip has no implicit target.
  setKey(NOTIFIABLE)
  await press('gatt-subscribe')
}

it('subscribes with the advertised mode and reports the actual mode', async () => {
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
  acceptDiscovery()
  await press('discover-gatt')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^none$/)
  setKey(READ_ONLY)
  expect(screen.getByTestId('gatt-subscribe')).toBeDisabled()
  expect(current.beginSubscription).not.toHaveBeenCalled()
  setKey(NOTIFIABLE)

  current.beginSubscription.mockReturnValue({
    notifications: {
      waitUntilEnabled: current.enableWait,
      nextEvent: current.nextEvent,
      beginDisable: current.beginDisable,
      detach: jest.fn(),
    } as unknown as BlengSubscription,
  })
  current.enableWait.mockResolvedValue({ mode: 'indication' })
  await press('gatt-subscribe')
  // The requested mode follows what the characteristic advertises.
  expect(current.beginSubscription).toHaveBeenCalledWith('requireNotification')
  expect(current.enableWait).toHaveBeenCalledWith(5000)
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^subscribed$/)
  expect(screen.getByTestId('notify-mode')).toHaveTextContent(/Actual mode: indication/)
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/notify-enable: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/notify-enable-wait: succeeded/)
})

it('detaches the lane after a refused enable and subscribes on the retry', async () => {
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
  acceptDiscovery()
  await press('discover-gatt')
  const detach = jest.fn()
  current.beginSubscription.mockReturnValue({
    notifications: {
      waitUntilEnabled: current.enableWait,
      nextEvent: current.nextEvent,
      beginDisable: current.beginDisable,
      detach,
    } as unknown as BlengSubscription,
  })
  const refused: BlengFailureInfo = {
    domain: 'operation', code: 'bleng.gatt.write_failed', message: 'ATT error',
    platform: { platform: 'android', code: '14', name: 'BluetoothGatt' },
  } as unknown as BlengFailureInfo
  current.enableWait.mockResolvedValueOnce({ failure: refused })
  setKey(NOTIFIABLE)
  await press('gatt-subscribe')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^failed$/)
  expect(screen.getByTestId('notify-error')).toHaveTextContent(/14/)
  // The admitted lane is abandoned, so nothing is left attached behind the retry.
  expect(detach).toHaveBeenCalledTimes(1)
  current.enableWait.mockResolvedValue({ mode: 'notification' })
  await press('gatt-subscribe')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^subscribed$/)
  expect(detach).toHaveBeenCalledTimes(1)
})

it('leaves the actual mode unreported instead of guessing it', async () => {
  await subscribed('unreported')
  expect(screen.getByTestId('notify-mode')).toHaveTextContent(/not reported/)
})

it('retires the subscription when the link ends instead of claiming it on a dead link', async () => {
  await subscribed()
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^subscribed$/)
  // The end arrives on the connection's own lane, with no command from this screen.
  await act(async () => { current.linkEnd.resolve({ ended: { reason: 'unrequested' } }) })
  expect(screen.getByTestId('link-state')).toHaveTextContent(/^disconnected$/)
  // A subscription cannot outlive its link: the phase ends with it, and a pull that could
  // only be answered over that link is withdrawn.
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^ended$/)
  expect(screen.getByTestId('pull-notification')).toBeDisabled()
  expect(screen.getByTestId('gatt-subscribe')).toBeDisabled()
})

it('counts values from the stream and keeps only the latest eight', async () => {
  await subscribed()
  for (let index = 0; index < 10; index += 1) {
    current.nextEvent.mockResolvedValue({ value: new Uint8Array([index]).buffer })
    await press('pull-notification')
  }
  expect(screen.getByTestId('notify-count')).toHaveTextContent(/10 values received; 2 omitted/)
  expect(current.nextEvent).toHaveBeenCalledTimes(10)
  // An empty value is a real notification, not a missing one.
  current.nextEvent.mockResolvedValue({ value: new ArrayBuffer(0) })
  await press('pull-notification')
  expect(screen.getByTestId('notify-count')).toHaveTextContent(/11 values received/)
})

it('keeps an observation timeout as an error without ending the subscription', async () => {
  await subscribed()
  current.nextEvent.mockResolvedValue({ failure: deadline })
  await press('pull-notification')
  expect(screen.getByTestId('notify-error')).toHaveTextContent(/observation\/bleng.timeout/)
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^subscribed$/)
  expect(screen.getByTestId('pull-notification')).toBeEnabled()
})

it('reports a refused disable as unverified, then resolves it from a delivered value', async () => {
  // Measured on the managed iPhone lane: the peer still has the CCC enabled, but the client
  // stopped delivering. Claiming `subscribed` after a refused disable would hide exactly
  // that divergence, so the phase reports what is actually known.
  await subscribed()
  current.beginDisable.mockReturnValue({
    disable: { wait: current.disableWait, cancel: jest.fn().mockResolvedValue({}) },
  })
  current.disableWait.mockResolvedValue({
    failure: {
      domain: 'operation',
      code: 'bleng.gatt.write_failed',
      message: 'ATT error',
      platform: { platform: 'apple', code: '14', name: 'CBATTErrorDomain' },
    } as unknown as BlengFailureInfo,
  })
  await press('disable-notifications')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^unverified$/)
  expect(screen.getByTestId('notify-error')).toHaveTextContent(/14/)
  // The controls stay usable: a failed disable is not a retired subscription.
  expect(screen.getByTestId('pull-notification')).toBeEnabled()

  // A value arrives: the subscription is live, and the refusal's detail is retired.
  current.nextEvent.mockResolvedValue({ value: new Uint8Array([0xe1]).buffer })
  await press('pull-notification')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^subscribed$/)
  expect(screen.queryByTestId('notify-error')).toBeNull()
})

it('keeps a refused disable unverified when no value arrives', async () => {
  await subscribed()
  current.beginDisable.mockReturnValue({
    disable: { wait: current.disableWait, cancel: jest.fn().mockResolvedValue({}) },
  })
  current.disableWait.mockResolvedValue({
    failure: {
      domain: 'operation',
      code: 'bleng.gatt.write_failed',
      message: 'ATT error',
      platform: { platform: 'apple', code: '14', name: 'CBATTErrorDomain' },
    } as unknown as BlengFailureInfo,
  })
  await press('disable-notifications')
  current.nextEvent.mockResolvedValue({ failure: deadline })
  await press('pull-notification')
  // A timeout is not proof that the subscription ended, so the phase stays unverified.
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^unverified$/)
  expect(screen.getByTestId('notify-error')).toHaveTextContent(/observation\/bleng.timeout/)
})

it('ends the stream on its own terminal and stops offering a pull', async () => {
  await subscribed()
  current.nextEvent.mockResolvedValue({ ended: { reason: 'connectionClosed' } })
  await press('pull-notification')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^ended$/)
  expect(screen.getByTestId('pull-notification')).toBeDisabled()
})

it('ends the stream on overflow instead of hiding the gap', async () => {
  await subscribed()
  current.nextEvent.mockResolvedValue({ ended: { reason: 'overflowed' } })
  await press('pull-notification')
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^ended$/)
  expect(screen.getByTestId('pull-notification')).toBeDisabled()
  expect(screen.getByTestId('notify-count')).toHaveTextContent(/0 values received/)
})

it('disables through the subscription capability and shows the receipt', async () => {
  await subscribed()
  current.beginDisable.mockReturnValue({
    disable: { wait: current.disableWait, cancel: jest.fn().mockResolvedValue({}) },
  })
  current.disableWait.mockResolvedValue({})
  expect(current.beginDisable).not.toHaveBeenCalled()
  await press('disable-notifications')
  expect(current.beginDisable).toHaveBeenCalledTimes(1)
  expect(current.disableWait).toHaveBeenCalledWith(5000)
  // A settled disable *is* the stream's terminal, so the phase publishes it and the lane
  // stops offering a pull: waiting for a later value would ask a closed lane for one.
  expect(screen.getByTestId('notify-state')).toHaveTextContent(/^ended$/)
  expect(screen.getByTestId('pull-notification')).toBeDisabled()
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/notify-disable: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/notify-disable-wait: succeeded/)
})

it('withdraws the subscription controls when the engine is closing', async () => {
  await subscribed()
  await press('close-session')
  expect(screen.getByTestId('pull-notification')).toBeDisabled()
  expect(screen.getByTestId('disable-notifications')).toBeDisabled()
  expect(screen.getByTestId('gatt-subscribe')).toBeDisabled()
})
