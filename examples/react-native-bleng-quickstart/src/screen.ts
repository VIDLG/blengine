/**
 * One screen's command layer: everything that *does* something, as plain functions.
 *
 * It lives outside the component for two reasons.
 *
 * 1. Capabilities are not render state. The central, the scan lane, the link and the subscription
 *    are retained handles with their own lifetimes; keeping them in this closure means a re-render
 *    cannot drop or duplicate one, and no `useRef` bookkeeping is needed.
 * 2. React Compiler only rewrites components and hooks. Command code that awaits, branches and
 *    recovers belongs in neither: kept as a plain function, the compiler skips it instead of
 *    silently giving up on the component that merely calls it.
 *
 * Every wait has a budget, every failure is read by `code` (never by `message`), and nothing here
 * retries: a retry is the caller's policy, so it stays visible in the screen.
 */
import {
  BlengOperationError, createBlengCentral, isBlengFailure, requireAccepted, requireOutcome, run,
} from '@blengine/react-native-bleng'
import type {
  BlengCentral, BlengConnection, BlengScan, BlengScanAdvertisement, BlengSubscription,
} from '@blengine/react-native-bleng'

/** One bounded budget for every wait on this screen. */
const TIMEOUT = 5_000
/** A quiet radio is not a failure: the pull simply expired and is worth repeating. */
const TIMEOUT_CODE = 'bleng.timeout'
/** Recent sightings, not a device inventory: a scan has no stable device identity to key on. */
const MAX_SIGHTINGS = 20

/** Everything the screen renders. Only facts the engine reported belong here. */
export interface Screen {
  adapter: string
  scanning: boolean
  devices: BlengScanAdvertisement[]
  link: 'none' | 'connecting' | 'connected' | 'ended'
  services: string[]
  value?: string
  values: number
  error?: string
}

export const initial: Screen = {
  adapter: 'starting', scanning: false, devices: [], link: 'none', services: [], values: 0,
}

/** How a controller reports. The caller owns the state primitive; the controller owns no state. */
export type ScreenDispatch = (change: (screen: Screen) => Screen) => void

/** The one place a failure becomes text: `code` for decisions, `message` for people. */
export function describe(cause: unknown): string {
  return cause instanceof BlengOperationError
    ? `${cause.code}: ${cause.message}`
    : cause instanceof Error ? cause.message : String(cause)
}

function bytes(value: ArrayBuffer): string {
  const view = new Uint8Array(value, 0, Math.min(16, value.byteLength))
  const shown = Array.from(view, byte => byte.toString(16).padStart(2, '0')).join(' ')
  return `${value.byteLength} bytes [${shown}]`
}

export interface ScreenController {
  /** Snapshot the adapter. Cheap: no radio access, no scan. */
  initialize(): Promise<void>
  /** End observation. Not a resource reset, and not a substitute for `disconnect`. */
  close(): Promise<void>
  startScan(): Promise<void>
  stopScan(): Promise<void>
  connect(advertisement: BlengScanAdvertisement): Promise<void>
  discoverAndRead(): Promise<void>
  subscribe(): Promise<void>
  disconnect(): Promise<void>
}

/**
 * Build the command layer for one screen.
 *
 * `dispatch` comes from the component's `useState` setter, which is referentially stable, so this
 * runs once per mounted screen and every handle below lives exactly as long as the screen does.
 */
export function createScreenController(dispatch: ScreenDispatch): ScreenController {
  const patch = (partial: Partial<Screen>): void => {
    dispatch(previous => ({ ...previous, ...partial }))
  }

  let central: BlengCentral | undefined
  let scanLane: BlengScan | undefined
  let connection: BlengConnection | undefined
  let subscription: BlengSubscription | undefined

  /**
   * A pull loop runs while its lane is still *this* lane.
   *
   * Clearing the handle before a teardown is what ends the loop: a loop parked in `nextEvent` then
   * wakes to an end or a timeout, sees it no longer owns the stream, and stops without reporting
   * the caller's own stop as an error.
   */
  const ownsScan = (lane: BlengScan): boolean => scanLane === lane
  const ownsSubscription = (lane: BlengSubscription): boolean => subscription === lane

  const pullAdvertisements = async (lane: BlengScan): Promise<void> => {
    while (ownsScan(lane)) {
      const next = await lane.nextEvent(TIMEOUT)
      // A stop or a restart may have happened while this pull was parked.
      if (!ownsScan(lane)) return
      if ('failure' in next) {
        if (next.failure.code === TIMEOUT_CODE) continue
        patch({ scanning: false, error: describe(new BlengOperationError(next.failure)) })
        return
      }
      if ('ended' in next) {
        scanLane = undefined
        patch({ scanning: false, error: `scan ended: ${next.ended.reason}` })
        return
      }
      // Sightings carry no device identity on purpose, so rows are keyed by position, not by peer.
      dispatch(previous => ({
        ...previous,
        devices: [next.advertisement, ...previous.devices].slice(0, MAX_SIGHTINGS),
      }))
    }
  }

  const pullValues = async (lane: BlengSubscription): Promise<void> => {
    while (ownsSubscription(lane)) {
      const next = await lane.nextEvent(TIMEOUT)
      // Disabling or dropping the link may have happened while this pull was parked.
      if (!ownsSubscription(lane)) return
      if ('failure' in next) {
        if (next.failure.code === TIMEOUT_CODE) continue
        patch({ error: describe(new BlengOperationError(next.failure)) })
        return
      }
      if ('ended' in next) return
      dispatch(previous => ({ ...previous, values: previous.values + 1 }))
    }
  }

  const initialize = async (): Promise<void> => {
    const created = createBlengCentral()
    central = created
    try {
      const { snapshot } = requireOutcome(await created.initialize())
      patch({
        adapter: `${snapshot.state} (scan ${snapshot.scanAuthorization}, `
          + `link ${snapshot.connectionAuthorization})`,
      })
    } catch (error) {
      patch({ adapter: 'failed', error: describe(error) })
    }
  }

  const close = async (): Promise<void> => {
    const created = central
    central = undefined
    // Ending observation also stops both pull loops: neither lane is current any more.
    scanLane = undefined
    subscription = undefined
    if (created) await created.close()
  }

  const startScan = async (): Promise<void> => {
    const current = central
    if (!current) return
    patch({ devices: [], scanning: true, error: undefined })
    try {
      const lane = requireAccepted(
        current.beginScan({ serviceUuids: [], allowDuplicates: false }),
      )
      const started = await lane.waitUntilStarted(TIMEOUT)
      // `waitUntilStarted` answers with a receipt: a failure is carried, not thrown. The lane is
      // retained only once it started, so a refusal here leaves nothing to stop.
      if (isBlengFailure(started)) {
        patch({ scanning: false, error: describe(new BlengOperationError(started.failure)) })
        return
      }
      scanLane = lane
      void pullAdvertisements(lane)
    } catch (error) {
      patch({ scanning: false, error: describe(error) })
    }
  }

  const stopScan = async (): Promise<void> => {
    const lane = scanLane
    if (!lane) {
      patch({ scanning: false })
      return
    }
    // Retire the handle first: the pull loop stops before the stop's own terminal arrives.
    scanLane = undefined
    try {
      // `beginStop()` is an operation like any other: one bounded wait, thrown on refusal.
      await run(lane.beginStop(), TIMEOUT)
    } catch (error) {
      patch({ error: describe(error) })
    }
    patch({ scanning: false })
  }

  const connect = async (advertisement: BlengScanAdvertisement): Promise<void> => {
    patch({ link: 'connecting', services: [], value: undefined, values: 0, error: undefined })
    try {
      const { connection: linked } = await run(advertisement.peripheral.beginConnect(), TIMEOUT)
      connection = linked
      patch({ link: 'connected' })
      // The end is retained, so a caller that starts listening late still learns about it.
      void linked.awaitEnd(120_000).then(next => {
        if ('ended' in next && connection === linked) {
          connection = undefined
          patch({ link: 'ended', error: `link ended: ${next.ended.reason}` })
        }
      })
    } catch (error) {
      patch({ link: 'none', error: describe(error) })
    }
  }

  const discoverAndRead = async (): Promise<void> => {
    const current = connection
    if (!current) return
    try {
      const { session } = await run(current.beginDiscoverGatt(), TIMEOUT)
      const services = session.services()
      patch({
        services: services.map(service => `${service.kind()} ${service.uuid()}`),
        value: undefined,
        error: undefined,
      })
      const characteristic = services
        .flatMap(service => service.characteristics())
        .find(item => item.properties().readable)
      if (!characteristic) {
        patch({ error: 'no readable characteristic on this peer' })
        return
      }
      const read = await run(characteristic.beginRead(), TIMEOUT)
      patch({ value: `${characteristic.uuid()}: ${bytes(read.value)}` })
    } catch (error) {
      patch({ error: describe(error) })
    }
  }

  const subscribe = async (): Promise<void> => {
    const current = connection
    if (!current) return
    try {
      const { session } = await run(current.beginDiscoverGatt(), TIMEOUT)
      const characteristic = session
        .services()
        .flatMap(service => service.characteristics())
        .find(item => item.properties().notifiable || item.properties().indicatable)
      if (!characteristic) {
        patch({ error: 'no notifiable characteristic on this peer' })
        return
      }
      // Ask for exactly what the peer advertises; the platform reports what it selected.
      const mode = characteristic.properties().notifiable
        ? 'requireNotification'
        : 'requireIndication'
      const lane = requireAccepted(characteristic.beginSubscription(mode))
      // `waitUntilEnabled` answers with a union, so its success arm is unwrapped here; the arm also
      // carries the mode the platform chose, which this screen does not need to show.
      requireOutcome(await lane.waitUntilEnabled(TIMEOUT))
      subscription = lane
      patch({ values: 0, error: undefined })
      void pullValues(lane)
    } catch (error) {
      patch({ error: describe(error) })
    }
  }

  const disconnect = async (): Promise<void> => {
    const current = connection
    if (!current) return
    // Disable first: a subscription outlives the link, so it needs its own terminal.
    const lane = subscription
    if (lane) {
      subscription = undefined
      try {
        await run(lane.beginDisable(), TIMEOUT)
      } catch (error) {
        patch({ error: describe(error) })
      }
    }
    try {
      await run(current.beginDisconnect(), TIMEOUT)
      connection = undefined
      patch({ link: 'none', services: [], values: 0 })
    } catch (error) {
      // A failed disconnect means "unknown", not "disconnected": keep the state honest.
      patch({ error: describe(error) })
    }
  }

  return {
    initialize, close, startScan, stopScan, connect, discoverAndRead, subscribe, disconnect,
  }
}
