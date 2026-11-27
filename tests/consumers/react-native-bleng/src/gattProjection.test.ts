import type {
  BlengCharacteristicProperties, BlengGattCharacteristic, BlengGattService, BlengGattSession,
} from '@blengine/react-native-bleng'
import { GATT_ADDRESSABLE_LIMIT, projectGatt } from './gattProjection'

const properties: BlengCharacteristicProperties = {
  readable: true,
  writableWithResponse: false,
  writableWithoutResponse: false,
  notifiable: false,
  indicatable: false,
}

function characteristic(uuid: string): BlengGattCharacteristic {
  return {
    uuid: () => uuid,
    properties: () => properties,
    descriptors: () => [],
  } as unknown as BlengGattCharacteristic
}

function service(uuid: string, uuids: string[]): BlengGattService {
  return {
    uuid: () => uuid,
    kind: () => 'primary',
    characteristics: () => uuids.map(characteristic),
  } as unknown as BlengGattService
}

function session(services: BlengGattService[]): BlengGattSession {
  return { services: () => services } as unknown as BlengGattSession
}

/** GAP-like, GATT-like, Device-Information-like, then the application's own service. */
function transportFirst(applicationUuids: string[]): BlengGattSession {
  return session([
    service('00001800', ['00002a00']),
    service('00001801', ['00002a05', '00002a06', '00002a07', '00002a08']),
    service('0000180a', ['00002a29', '00002a24', '00002a25', '00002a27']),
    service('fd600000', applicationUuids),
  ])
}

it('keeps the whole realistic topology addressable at the screen limit', () => {
  const uuids = ['fff10000', 'fff20000', 'fff50000', 'fff30000', 'fff40000']
  const projected = projectGatt(transportFirst(uuids))
  expect(projected.characteristics).toBe(14)
  expect(projected.omitted).toBe(0)
  expect(projected.addressable.map(entry => entry.uuid)).toEqual([
    '00002a00',
    '00002a05', '00002a06', '00002a07', '00002a08',
    '00002a29', '00002a24', '00002a25', '00002a27',
    ...uuids,
  ])
  expect(projected.services[3].characteristics.map(entry => entry.uuid)).toEqual(uuids)
})

it('spends a tight budget across services before deepening any one service', () => {
  const projected = projectGatt(transportFirst(['fff20000', 'fff50000']), 6)
  // One slot per service, then the two services with a second characteristic.
  expect(projected.addressable.map(entry => entry.key)).toEqual([
    '0.0', '1.0', '1.1', '2.0', '2.1', '3.0',
  ])
  expect(projected.addressable.map(entry => entry.uuid)).toContain('fff20000')
  expect(projected.omitted).toBe(5)
})

it('never leaves a discovered service without an addressable characteristic', () => {
  const projected = projectGatt(transportFirst(['fff20000', 'fff50000']), 4)
  expect(projected.services.map(entry => entry.characteristics.length)).toEqual([1, 1, 1, 1])
  expect(projected.addressable).toHaveLength(4)
})

it('retains a contiguous key prefix per service so row probes never skip a row', () => {
  const projected = projectGatt(transportFirst(['fff20000', 'fff50000']), 7)
  for (const entry of projected.services) {
    expect(entry.characteristics.map(row => row.key)).toEqual(
      entry.characteristics.map((_, index) => `${entry.key}.${index}`),
    )
  }
  expect(projected.omitted).toBe(4)
})

it('states a limit large enough for the backend discovery bound to be the real cap', () => {
  expect(GATT_ADDRESSABLE_LIMIT).toBeGreaterThan(32)
})
