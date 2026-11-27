// The package barrel imports the Nitro bridge at module load. Only the native boundaries are
// stubbed; `shareLane` itself is the real pure-JS implementation.
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
const { shareLane } = jest.requireActual<typeof import('@blehub/react-native-bleng')>(
  '@blehub/react-native-bleng',
)

type LaneEvent =
  | { value: number }
  | { ended: { reason: string } }
  | { failure: { code: string } }

function laneWith(events: LaneEvent[], hangWhenExhausted = false) {
  let index = 0
  return {
    nextEvent: jest.fn(async (_timeoutMs: number): Promise<LaneEvent> => {
      const next = events[index]
      index += 1
      if (next !== undefined) {
        return next
      }
      if (hangWhenExhausted) {
        return new Promise<LaneEvent>(() => {})
      }
      return { ended: { reason: 'exhausted' } }
    }),
  }
}

async function flush(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

describe('shareLane', () => {
  it('keeps one pull loop and fans every event out to every subscriber', async () => {
    const lane = laneWith([{ value: 1 }, { value: 2 }, { ended: { reason: 'requested' } }])
    const shared = shareLane(lane, { timeoutMs: 10 })
    const first: LaneEvent[] = []
    const second: LaneEvent[] = []
    shared.subscribe((event) => first.push(event))
    shared.subscribe((event) => second.push(event))

    await flush()

    expect(lane.nextEvent).toHaveBeenCalledTimes(3)
    expect(first).toEqual([
      { value: 1 },
      { value: 2 },
      { ended: { reason: 'requested' } },
    ])
    expect(second).toEqual(first)
  })

  it('does not deliver an idle timeout as an event', async () => {
    const lane = laneWith([
      { failure: { code: 'bleng.timeout' } },
      { value: 1 },
      { ended: { reason: 'requested' } },
    ])
    const shared = shareLane(lane, { timeoutMs: 10 })
    const seen: LaneEvent[] = []
    shared.subscribe((event) => seen.push(event))

    await flush()

    expect(seen).toEqual([{ value: 1 }, { ended: { reason: 'requested' } }])
  })

  it('replays a retained terminal to a late subscriber', async () => {
    const lane = laneWith([{ value: 1 }, { ended: { reason: 'requested' } }])
    const shared = shareLane(lane, { timeoutMs: 10 })
    const late = jest.fn()

    await flush()
    shared.subscribe(late)

    expect(late).toHaveBeenCalledTimes(1)
    expect(late).toHaveBeenCalledWith({ ended: { reason: 'requested' } })
  })

  it('stops pulling after stop without claiming cancellation', async () => {
    const lane = laneWith([{ value: 1 }, { value: 2 }], true)
    const shared = shareLane(lane, { timeoutMs: 10 })
    const seen: LaneEvent[] = []
    shared.subscribe((event) => seen.push(event))
    await flush()
    const pulls = lane.nextEvent.mock.calls.length

    shared.close()
    await flush()

    expect(lane.nextEvent.mock.calls.length).toBe(pulls)
    expect(seen).toEqual([{ value: 1 }, { value: 2 }])
  })
})
