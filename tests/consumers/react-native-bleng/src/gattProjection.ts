import type {
  BlengCharacteristicProperties, BlengGattCharacteristic, BlengGattDescriptor, BlengGattSession,
  BlengServiceKind,
} from '@blehub/react-native-bleng'

/**
 * UI projection of one discovered topology.
 *
 * Every `key` below is derived from the position in the discovery snapshot, not from a native
 * handle. Only a bounded number of characteristic capabilities are retained, because a capability
 * keeps native state alive; `omitted` states how many the screen cannot address.
 */
export interface GattCharacteristicEntry {
  key: string
  uuid: string
  properties: BlengCharacteristicProperties
  descriptors: string[]
  /**
   * Descriptor capabilities, bounded. A descriptor is the only way to read or write an
   * attribute the characteristic's value API does not address — notably the CCCD, whose value
   * is the peer's own answer about whether a subscription is enabled.
   */
  descriptorCapabilities: BlengGattDescriptor[]
  capability: BlengGattCharacteristic
}

export interface GattServiceEntry {
  key: string
  uuid: string
  kind: BlengServiceKind
  characteristicCount: number
  characteristics: GattCharacteristicEntry[]
}

export interface GattProjection {
  services: GattServiceEntry[]
  characteristics: number
  omitted: number
  /**
   * Flat, bounded list the screen may address; a subset of what discovery returned, in discovery
   * order.
   */
  addressable: GattCharacteristicEntry[]
}

export const GATT_ADDRESSABLE_LIMIT = 64
/**
 * Descriptor capabilities retained per characteristic.
 *
 * Real peers expose one or two (the CCCD, sometimes a user description); a bound keeps a
 * pathological database from retaining unbounded native state, and a scene that needs a
 * descriptor beyond it is a scene the screen cannot address.
 */
export const GATT_DESCRIPTOR_LIMIT = 8

/**
 * Project a discovered topology onto a bounded, addressable UI.
 *
 * `limit` bounds only what the screen renders and lets the user act on: the backends already bound
 * discovery itself (Apple retains at most 1024 characteristics), so the screen bound exists to keep
 * the view tree and the retained capability list small, not to describe the peer. It is set above
 * any realistic GATT database so a real device's topology is addressable in full.
 *
 * The bound is spent per service first: every service that has characteristics is represented
 * before any service receives a second slot, and so on. Truncating by discovery order instead would
 * let a transport that lists its standard services (GAP, GATT, Device Information) first consume
 * the whole budget, leaving an application service discovered later entirely unreachable even
 * though discovery succeeded.
 *
 * A service's retained characteristics are always the prefix of its discovered ones, so a key index
 * of `n` implies the keys `0..n-1` exist for that service.
 */
export function projectGatt(
  session: BlengGattSession,
  limit: number = GATT_ADDRESSABLE_LIMIT,
): GattProjection {
  const discovered = session.services().map(service => ({
    service, characteristics: service.characteristics(),
  }))
  const characteristics = discovered.reduce(
    (total, entry) => total + entry.characteristics.length, 0,
  )
  const retained = discovered.map(() => [] as GattCharacteristicEntry[])
  let retainedCount = 0
  const deepest = discovered.reduce(
    (depth, entry) => Math.max(depth, entry.characteristics.length), 0,
  )
  for (let index = 0; index < deepest && retainedCount < limit; index += 1) {
    for (const [serviceIndex, entry] of discovered.entries()) {
      if (retainedCount >= limit) break
      const capability = entry.characteristics[index]
      if (capability === undefined) continue
      const descriptors = capability.descriptors().slice(0, GATT_DESCRIPTOR_LIMIT)
      retained[serviceIndex].push({
        key: `${serviceIndex}.${index}`,
        uuid: capability.uuid(),
        properties: capability.properties(),
        descriptorCapabilities: descriptors,
        descriptors: descriptors.map(descriptor => descriptor.uuid()),
        capability,
      })
      retainedCount += 1
    }
  }
  return {
    services: discovered.map((entry, serviceIndex) => ({
      key: String(serviceIndex),
      uuid: entry.service.uuid(),
      kind: entry.service.kind(),
      characteristicCount: entry.characteristics.length,
      characteristics: retained[serviceIndex],
    })),
    characteristics,
    omitted: characteristics - retainedCount,
    addressable: retained.flat(),
  }
}

/** One-line evidence text; the platform-reported properties in a fixed order. */
export function gattCharacteristicText(entry: GattCharacteristicEntry): string {
  return `${entry.uuid} [${gattPropertyFlags(entry)}]`
}

export type GattTargetStatus = 'none' | 'invalid' | 'ambiguous' | 'one'

/**
 * Resolve a *declared* characteristic UUID against this run's bounded topology.
 *
 * The screen addresses characteristics by the UUID a caller declares, not by the row key
 * its discovery position happens to produce. A row key is an artifact of discovery order:
 * an operator would have to read the index and copy a position, and a caller would have to
 * assume that position means the same thing on another peer. A UUID is the identity the
 * caller actually knows, and this projection is the only thing that knows the mapping.
 *
 * Exactly one match is an answer. Zero matches, an unparsable declaration and several
 * matches are all refusals, because acting on the wrong characteristic is worse than not
 * acting: each is reported, never guessed.
 */
export function resolveGattTarget(
  gatt: GattProjection | undefined,
  declared: string,
): { status: GattTargetStatus; entry?: GattCharacteristicEntry; matches: number } {
  const target = declared.trim().toLowerCase()
  if (target === '') return { status: 'none', matches: 0 }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(target)) {
    return { status: 'invalid', matches: 0 }
  }
  const matches = (gatt?.addressable ?? []).filter(
    entry => entry.uuid.toLowerCase() === target,
  )
  if (matches.length === 0) return { status: 'none', matches: 0 }
  if (matches.length > 1) return { status: 'ambiguous', matches: matches.length }
  return { status: 'one', entry: matches[0], matches: 1 }
}

/** The platform-reported capabilities of one entry, in a stable machine-readable order. */
export function gattPropertyFlags(entry: GattCharacteristicEntry): string {
  const { properties } = entry
  return [
    properties.readable ? 'read' : undefined,
    properties.writableWithResponse ? 'write' : undefined,
    properties.writableWithoutResponse ? 'writeNoResponse' : undefined,
    properties.notifiable ? 'notify' : undefined,
    properties.indicatable ? 'indicate' : undefined,
  ].filter((flag): flag is string => flag !== undefined).join(',') || 'none'
}
