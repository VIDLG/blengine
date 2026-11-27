import type { BlengCentral } from '@blehub/react-native-bleng'

const reactNativeMock = {
  Platform: { OS: 'ios' },
  TurboModuleRegistry: { getEnforcing: jest.fn() },
}
const nitroMock = {
  NitroModules: { createHybridObject: jest.fn() },
}
const resolveModule = (require as typeof require & {
  resolve(name: string, options?: { paths: string[] }): string
}).resolve
const { dirname } = require('node:path') as { dirname(path: string): string }
const consumerDirectory = dirname(resolveModule('../package.json'))
const packageSourceDirectory = dirname(resolveModule('@blehub/react-native-bleng'))

// The consumer and package source have separate node_modules trees. Register both exact
// resolutions before loading the real factory, sharing the mocks used by the assertions.
for (const directory of [consumerDirectory, packageSourceDirectory]) {
  jest.doMock(resolveModule('react-native', { paths: [directory] }), () => reactNativeMock)
  jest.doMock(resolveModule('react-native-nitro-modules', { paths: [directory] }), () => nitroMock)
}
const { Platform, TurboModuleRegistry } = require('react-native') as typeof import('react-native')
const { NitroModules } = require('react-native-nitro-modules') as typeof import('react-native-nitro-modules')
const { createBlengCentral } = jest.requireActual<typeof import('@blehub/react-native-bleng')>(
  '@blehub/react-native-bleng',
)

const getLifecycle = jest.mocked(TurboModuleRegistry.getEnforcing)
const createHybridObject = jest.mocked(NitroModules.createHybridObject)
const binderName = '__blehubBindBlengCentral'
const originalBinder = Object.getOwnPropertyDescriptor(globalThis, binderName)
let central: BlengCentral

function installBinder(value: unknown) {
  Object.defineProperty(globalThis, binderName, { value, configurable: true })
}

beforeEach(() => {
  jest.resetAllMocks()
  jest.replaceProperty(Platform, 'OS', 'ios')
  delete (globalThis as Record<string, unknown>)[binderName]
  central = { initialize: jest.fn(), beginScan: jest.fn(), close: jest.fn() } as unknown as BlengCentral
  createHybridObject.mockReturnValue(central)
  getLifecycle.mockReturnValue({})
})

afterEach(() => {
  jest.restoreAllMocks()
  if (originalBinder) Object.defineProperty(globalThis, binderName, originalBinder)
  else delete (globalThis as Record<string, unknown>)[binderName]
})

it('materializes the iOS module before construction and binds the actual Nitro object before returning', () => {
  const order: string[] = []
  const bindCentral = jest.fn((actual: BlengCentral) => {
    order.push('bind')
    expect(actual).toBe(central)
  })
  getLifecycle.mockImplementation(() => {
    order.push('module')
    installBinder(bindCentral)
    return {}
  })
  createHybridObject.mockImplementation(() => {
    order.push('create')
    return central
  })

  expect(createBlengCentral()).toBe(central)
  order.push('returned')
  expect(order).toEqual(['module', 'create', 'bind', 'returned'])
  expect(getLifecycle).toHaveBeenCalledWith('BleHubBlengEngineLifecycle')
  expect(getLifecycle).toHaveBeenCalledTimes(1)
  expect(createHybridObject).toHaveBeenCalledWith('BlengCentral')
  expect(createHybridObject).toHaveBeenCalledTimes(1)
  expect(bindCentral).toHaveBeenCalledTimes(1)
  expect(central.initialize).not.toHaveBeenCalled()
  expect(central.close).not.toHaveBeenCalled()
})

it('fails before constructing an iOS central when the lifecycle module is missing, even with a stale binder', () => {
  const missingModule = new Error('lifecycle TurboModule unavailable')
  const staleBinder = jest.fn()
  installBinder(staleBinder)
  getLifecycle.mockImplementation(() => { throw missingModule })

  expect(() => createBlengCentral()).toThrow(missingModule)
  expect(createHybridObject).not.toHaveBeenCalled()
  expect(staleBinder).not.toHaveBeenCalled()
})

it.each([undefined, null, {}, 'not a function'])('fails before constructing an iOS central with invalid binder %p', binder => {
  installBinder(binder)

  expect(() => createBlengCentral()).toThrow('BLeng iOS engine lifecycle binding is unavailable')
  expect(getLifecycle).toHaveBeenCalledWith('BleHubBlengEngineLifecycle')
  expect(createHybridObject).not.toHaveBeenCalled()
})

it('does not return an iOS central when native binding rejects it', () => {
  const rejected = new Error('duplicate, rebound, or retired engine')
  const bindCentral = jest.fn(() => { throw rejected })
  installBinder(bindCentral)

  expect(() => createBlengCentral()).toThrow(rejected)
  expect(bindCentral).toHaveBeenCalledWith(central)
  expect(createHybridObject).toHaveBeenCalledTimes(1)
  expect(central.initialize).not.toHaveBeenCalled()
  expect(central.close).not.toHaveBeenCalled()
})

it('binds each newly constructed iOS central rather than caching an central or skipping admission', () => {
  const nextOwner = {} as BlengCentral
  const bindCentral = jest.fn()
  installBinder(bindCentral)
  createHybridObject.mockReturnValueOnce(central).mockReturnValueOnce(nextOwner)

  expect(createBlengCentral()).toBe(central)
  expect(createBlengCentral()).toBe(nextOwner)
  expect(getLifecycle).toHaveBeenCalledTimes(2)
  expect(bindCentral.mock.calls).toEqual([[central], [nextOwner]])
})

it('propagates Nitro construction failure without calling the iOS binder', () => {
  const failed = new Error('Nitro construction failed')
  const bindCentral = jest.fn()
  installBinder(bindCentral)
  createHybridObject.mockImplementation(() => { throw failed })

  expect(() => createBlengCentral()).toThrow(failed)
  expect(bindCentral).not.toHaveBeenCalled()
})

it.each([false, true])('leaves Android construction unchanged with an iOS binder present: %p', withBinder => {
  jest.replaceProperty(Platform, 'OS', 'android')
  const bindCentral = jest.fn(() => { throw new Error('iOS binder must not run on Android') })
  if (withBinder) installBinder(bindCentral)
  getLifecycle.mockImplementation(() => { throw new Error('iOS module must not load on Android') })

  expect(createBlengCentral()).toBe(central)
  expect(createHybridObject).toHaveBeenCalledWith('BlengCentral')
  expect(createHybridObject).toHaveBeenCalledTimes(1)
  expect(getLifecycle).not.toHaveBeenCalled()
  expect(bindCentral).not.toHaveBeenCalled()
})
