import type { BlengFailureInfo } from '@blehub/react-native-bleng'

// Same pure-JS boundary stubs as `pending.test.ts`: load the real source, no native module.
const reactNativeMock = {
  Platform: { OS: 'ios' },
  TurboModuleRegistry: { getEnforcing: jest.fn() },
}
const nitroMock = { NitroModules: { createHybridObject: jest.fn() } }
const resolveModule = (require as typeof require & {
  resolve(name: string, options?: { paths: string[] }): string
}).resolve
const { dirname } = require('node:path') as { dirname(path: string): string }
const consumerDirectory = dirname(resolveModule('../package.json'))
const packageSourceDirectory = dirname(resolveModule('@blehub/react-native-bleng'))
for (const directory of [consumerDirectory, packageSourceDirectory]) {
  jest.doMock(resolveModule('react-native', { paths: [directory] }), () => reactNativeMock)
  jest.doMock(resolveModule('react-native-nitro-modules', { paths: [directory] }), () => nitroMock)
}
const { BlengOperationError, failureReason } =
  jest.requireActual<typeof import('@blehub/react-native-bleng')>('@blehub/react-native-bleng')

function failure(code: string, message = 'native display text'): BlengFailureInfo {
  return { domain: 'submission', code, message }
}

it('maps the stable code suffix, not the full path', () => {
  expect(failureReason(failure('bleng.dispatch_rejected.not_supported'))).toBe(
    'The platform does not support this operation.',
  )
  // Delivery codes are emitted by the supervisor as `delivery.<snake_case>`; the table must use
  // that spelling or every lane failure silently falls back to the native message.
  expect(failureReason(failure('delivery.byte_overflow'))).toBe(
    'The bounded byte buffer overflowed and ended the lane.',
  )
  expect(failureReason(failure('delivery.pull_busy'))).toBe(
    'Another pull is already pending on this lane.',
  )
})

it('falls back to the native message for an unknown code', () => {
  // A newly added code must stay displayable without a package release.
  expect(failureReason(failure('bleng.future_category', 'future native detail'))).toBe(
    'future native detail',
  )
})

it('exposes the same reason on the raised error without changing code', () => {
  const error = new BlengOperationError(failure('bleng.timeout', 'timed out natively'))
  expect(error.code).toBe('bleng.timeout')
  expect(error.reason).toBe('The operation timed out.')
  expect(error.message).toBe('timed out natively')
})
