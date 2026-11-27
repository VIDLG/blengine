/**
 * Multicast one native lane without a second native pull.
 *
 * A lane is single-pending-pull by contract. Two screens that both need scan advertisements (or
 * notification values) should share this helper instead of racing two `nextEvent` calls. It keeps
 * one pull loop, fans events out synchronously, and keeps draining even when there are no
 * subscribers so the native bounded queue cannot be filled by a temporarily unmounted view.
 *
 * This is a derived convenience layer. It never invents an end or a failure, never cancels native
 * work, and never changes a timeout into a link-state claim.
 */

interface LaneEventShape {
  ended?: unknown
  failure?: { code?: string }
}

export interface ShareableLane<TEvent> {
  nextEvent(timeoutMs: number): Promise<TEvent>
}

export interface SharedLane<TEvent> {
  /** Add one listener. Returns an unsubscribe function; unsubscribing never stops the lane. */
  subscribe(listener: (event: TEvent) => void): () => void
  /** Close this local fan-out. A pending native pull stays pending until its own timeout. */
  close(): void
}

/**
 * Share a lane's events with several consumers.
 *
 * `bleng.timeout` is the caller's idle bound, not a terminal, so it is not delivered. Any other
 * `failure` or an `ended` event is terminal: it is delivered once, then replayed to late
 * subscribers because a stream's end is a retained fact.
 */
export function shareLane<TEvent>(
  lane: ShareableLane<TEvent>,
  options: { timeoutMs?: number } = {},
): SharedLane<TEvent> {
  const listeners = new Set<(event: TEvent) => void>()
  const timeoutMs = options.timeoutMs ?? 30_000
  let stopped = false
  let terminal: TEvent | null = null

  const emit = (event: TEvent) => {
    for (const listener of [...listeners]) {
      listener(event)
    }
  }

  void (async () => {
    while (!stopped) {
      let event: TEvent
      try {
        event = await lane.nextEvent(timeoutMs)
      } catch {
        // A rejected pull is not a BLE fact. Stop instead of manufacturing an event.
        return
      }
      if (stopped) {
        return
      }
      const shaped = event as LaneEventShape
      if (shaped.failure?.code === 'bleng.timeout') {
        continue
      }
      emit(event)
      if (shaped.ended !== undefined || shaped.failure !== undefined) {
        terminal = event
        return
      }
    }
  })()

  return {
    subscribe(listener) {
      if (terminal !== null) {
        listener(terminal)
        return () => {}
      }
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    close() {
      stopped = true
      listeners.clear()
    },
  }
}
