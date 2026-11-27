import { encodeRunStatus, runStatusFacts, RUN_STATUS_KEYS, type RunStatusInput } from './runStatus'
import { parseScanFilter } from './useManualScan'

const input = (overrides: Partial<RunStatusInput> = {}): RunStatusInput => ({
  state: 'idle',
  page: 'Tests',
  view: {
    phase: 'idle', link: 'none', linkEnd: 'none', gattPhase: 'none', gattRead: 'none',
    notifyPhase: 'none', total: 0, notifyTotal: 0,
    mtuPhase: 'none', mtuRequestPhase: 'none', priorityPhase: 'none', refreshPhase: 'none',
    descriptorPhase: 'none', descriptorWritePhase: 'none', deviceQueryPhase: 'none',
    autoPhase: 'none', radioPhase: 'none',
  },
  timeoutText: '5000',
  filterText: '',
  gattTarget: '',
  gattMatch: 'none',
  gattProps: 'none',
  gattReads: 0,
  gattReadTotal: 0,
  descriptorReadTotal: 0,
  ...overrides,
})

const decode = (text: string): Record<string, string> =>
  Object.fromEntries(text.split(';').map(field => {
    const index = field.indexOf('=')
    return [field.slice(0, index), field.slice(index + 1)]
  }))

it('encodes every declared field exactly once, in the declared order', () => {
  const keys = RUN_STATUS_KEYS.map(key => key)
  expect(keys).toEqual([
    'state', 'page', 'scan', 'link', 'linkEnd', 'gatt', 'notify',
    'scanTotal', 'notifyTotal', 'notifyMode', 'gattRead', 'gattReads',
    'scanTimeout', 'scanFilter', 'gattTarget', 'gattMatch', 'gattProps', 'gattReadTotal',
    'mtu', 'mtuWrites', 'mtuRequest', 'priority', 'refresh',
    'descriptor', 'descriptorValue', 'descriptorWrite', 'descriptorReadTotal',
    'devices', 'devicesCount', 'devicesPeer', 'devicesKind', 'auto',
    'radio', 'radioTarget',
    'failure',
  ])
  const text = encodeRunStatus(runStatusFacts(input()))
  expect(text.split(';').map(field => field.split('=')[0])).toEqual(keys)
  expect(decode(text)).toMatchObject({ state: 'idle', page: 'Tests', scan: 'idle' })
})

it('reports an unusable input as unknown rather than as a usable value', () => {
  // A declared value the App cannot honour is `unknown`; a value it does not have is
  // `none`. A scene that needs the value must fail either way instead of guessing.
  expect(decode(encodeRunStatus(runStatusFacts(input({ timeoutText: 'soon' }))))!.scanTimeout).toBe('unknown')
  expect(decode(encodeRunStatus(runStatusFacts(input({ filterText: 'not-a-uuid' }))))!.scanFilter).toBe('unknown')
  expect(decode(encodeRunStatus(runStatusFacts(input())))!.scanTimeout).toBe('5000')
})

it('reports a subscription without a mode as unknown, never as none', () => {
  const view = input().view
  const none = decode(encodeRunStatus(runStatusFacts(input())))!
  const silent = decode(encodeRunStatus(runStatusFacts(input({
    view: { ...view, notifyPhase: 'subscribed' },
  }))))!
  const reported = decode(encodeRunStatus(runStatusFacts(input({
    view: { ...view, notifyPhase: 'subscribed', notifyMode: 'indication' },
  }))))!
  expect(none.notifyMode).toBe('none')
  expect(silent.notifyMode).toBe('unknown')
  expect(reported.notifyMode).toBe('indication')
})

it('states the scan filter in the same canonical form the host declares', () => {
  const text = input({ filterText: ' FFF20000-0000-4000-8000-000000000000 , fff10000-0000-4000-8000-000000000000 ,fff20000-0000-4000-8000-000000000000' })
  const declared = decode(encodeRunStatus(runStatusFacts(text)))!.scanFilter
  expect(declared).toBe(
    'fff20000-0000-4000-8000-000000000000,fff10000-0000-4000-8000-000000000000',
  )
  // The projection and the human summary cannot disagree about the same request.
  expect(parseScanFilter(text.filterText).uuids).toEqual(declared.split(','))
})

it('keeps a separator-bearing value from making the whole projection unparsable', () => {
  const text = encodeRunStatus(runStatusFacts(input({ gattTarget: 'x;state=started' })))
  const fields = decode(text)
  expect(fields.state).toBe('idle')
  expect(fields.gattTarget).toBe('x state=started')
  expect(text.split(';')).toHaveLength(RUN_STATUS_KEYS.length)
})

it('distinguishes an end nobody here asked for from the one this App requested', () => {
  const view = input().view
  const decodeView = (linkEnd: string): string =>
    decode(encodeRunStatus(runStatusFacts(input({ view: { ...view, linkEnd, link: 'disconnected' } }))))!
      .linkEnd
  // A scene asserts which end happened; the difference is a scalar, not a sentence.
  expect(decodeView('unrequested')).toBe('unrequested')
  expect(decodeView('requested')).toBe('requested')
  expect(decode(encodeRunStatus(runStatusFacts(input())))!.linkEnd).toBe('none')
})

it('reports a failure as an identity, never as the sentence beside it', () => {
  const refused = decode(encodeRunStatus(runStatusFacts(input({
    failureText: 'gatt/gatt: ATT error [Android/14: BluetoothGatt]',
  }))))!
  expect(refused.failure).toBe('android/14')
  // A failure with no platform answer still names its own domain and code.
  const local = decode(encodeRunStatus(runStatusFacts(input({
    failureText: 'observation/bleng.timeout: BLeng operation timed out',
  }))))!
  expect(local.failure).toBe('observation/bleng.timeout')
  // A run that shows no failure says so, and an unreadable one is never `none`.
  expect(decode(encodeRunStatus(runStatusFacts(input())))!.failure).toBe('none')
  expect(decode(encodeRunStatus(runStatusFacts(input({ failureText: '!!' }))))!.failure)
    .toBe('unknown')
})

it('reports how a declared characteristic resolved, never a position', () => {
  const bare = decode(encodeRunStatus(runStatusFacts(input())))!
  const one = decode(encodeRunStatus(runStatusFacts(input({
    gattTarget: ' FFF20000-0000-4000-8000-000000000000 ',
    gattMatch: 'one', gattProps: 'read,notify,indicate',
  }))))!
  expect(bare.gattTarget).toBe('none')
  expect(bare.gattMatch).toBe('none')
  // Declared identity is canonicalised the same way the host declares it.
  expect(one.gattTarget).toBe('fff20000-0000-4000-8000-000000000000')
  expect(one.gattMatch).toBe('one')
  expect(one.gattProps).toBe('read,notify,indicate')
})
