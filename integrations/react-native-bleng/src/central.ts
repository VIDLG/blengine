import { Platform, TurboModuleRegistry } from 'react-native'
import type { TurboModule } from 'react-native'
import { NitroModules } from 'react-native-nitro-modules'
import type { BlengCloseResult, BlengCentral } from '../specs/BlengCentral.nitro'

type BlengRuntime = typeof globalThis & {
  __blehubBindBlengCentral?: (central: BlengCentral) => void
  __blehubBlengCentral?: BlengCentral
}

/** Typed Apple/Android bridge; construction does not initialize BLE. */
export function createBlengCentral(): BlengCentral {
  if (Platform.OS === 'ios') {
    // Materializing the module installs bindings for this runtime, not a process-global central.
    TurboModuleRegistry.getEnforcing<TurboModule>('BleHubBlengEngineLifecycle')
    const bindCentral = (globalThis as BlengRuntime).__blehubBindBlengCentral
    if (typeof bindCentral !== 'function') {
      throw new Error('BLeng iOS engine lifecycle binding is unavailable')
    }
    const central = NitroModules.createHybridObject<BlengCentral>('BlengCentral')
    bindCentral(central)
    return central
  }
  return NitroModules.createHybridObject<BlengCentral>('BlengCentral')
}

/**
 * The JS-runtime-scoped central used by `BlengProvider`.
 *
 * The slot lives on `globalThis`, so React Fast Refresh re-evaluates modules without creating a
 * second central or dropping live capabilities. A full reload is a new JS runtime: RN invalidates
 * the old native engine, so the app must reconnect; this helper cannot and must not resurrect it.
 */
export function getBlengCentral(): BlengCentral {
  const runtime = globalThis as BlengRuntime
  const existing = runtime.__blehubBlengCentral
  if (existing !== undefined) {
    return existing
  }
  const central = createBlengCentral()
  runtime.__blehubBlengCentral = central
  return central
}

/**
 * Closes the runtime-scoped central and clears the slot only when it reports closure.
 *
 * This is explicit teardown, not an unmount hook: a provider or screen unmount must not close the
 * engine, or navigation and Fast Refresh would tear down live BLE operations. Calling
 * `central.close()` directly leaves this slot pointing at a closed central, so use this helper for
 * the runtime-scoped central.
 */
export async function closeBlengCentral(): Promise<BlengCloseResult> {
  const runtime = globalThis as BlengRuntime
  const central = runtime.__blehubBlengCentral
  if (central === undefined) {
    return { closed: true }
  }
  const result = await central.close()
  if (result.closed) {
    delete runtime.__blehubBlengCentral
  }
  return result
}
