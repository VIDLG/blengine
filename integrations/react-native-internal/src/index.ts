import { NitroModules } from 'react-native-nitro-modules'
import type { BleHubProbe } from '../specs/BleHubProbe.nitro'

/** Internal-only Nitro codegen probe; not a public RN API. */
export function createBleHubProbe(): BleHubProbe {
  return NitroModules.createHybridObject<BleHubProbe>('BleHubProbe')
}
