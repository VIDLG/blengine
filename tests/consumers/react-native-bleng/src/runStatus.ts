import { parseScanFilter } from './useManualScan'

/**
 * The workbench's machine-readable status projection.
 *
 * A UI scene polls far more often than it acts: every wait, every click precondition
 * and every pre-dispatch recheck needs the same handful of scalar facts. Reading those
 * as separate elements costs one remote request each, and — more importantly — lets a
 * caller combine facts that never held at the same instant.
 *
 * `run-status` is therefore ONE pinned text node carrying every scalar fact a scene
 * needs, encoded as `key=value` pairs joined by `;`. This module owns the encoding.
 * The decoding half lives in `tools/react_native/run_bleng_ui.py`, and the contract
 * both sides implement is stated in `tests/consumers/README.md`.
 *
 * Deliberately excluded: errors, result lines, topology rows and sample text. Those are
 * free-form observations that belong in their own elements, so this projection can
 * never become a place where a diagnostic masquerades as a result.
 */
export const RUN_STATUS_KEYS = [
  'state', 'page', 'scan', 'link', 'linkEnd', 'gatt', 'notify',
  'scanTotal', 'notifyTotal', 'notifyMode', 'gattRead', 'gattReads',
  'scanTimeout', 'scanFilter', 'gattTarget', 'gattMatch', 'gattProps', 'gattReadTotal',
  // Link facts: each is `none|pending|done|unsupported|failed`. `unsupported` is a platform
  // answer, not a failure — a scene asserts the difference, and a platform that cannot do it
  // must be visible as itself rather than as a timeout.
  'mtu', 'mtuWrites', 'mtuRequest', 'priority', 'refresh',
  // The CCCD as the peer stores it: `descriptor` is the terminal, `descriptorValue` its
  // bytes, the write is reported separately because it is a different command, and
  // `descriptorReadTotal` is the monotonic settled-read count a scene advances to prove a
  // *fresh* read instead of one whose value matches a carried-over previous read.
  'descriptor', 'descriptorValue', 'descriptorWrite', 'descriptorReadTotal',
  // The system inventory across a live link: the query's terminal, how many entries it
  // returned, and whether the peer this run is linked to appears among them. A count alone
  // would not say the inventory describes *this* link.
  'devices', 'devicesCount', 'devicesPeer', 'devicesKind',
  // The standing "connect when the peer appears" lane: `none|pending|done|unsupported|failed`.
  // `unsupported` is a platform answer (CoreBluetooth has no auto-connect), not a failure.
  'auto',
  // Radio control: the terminal, and which direction was asked. `unsupported` is the expected
  // answer on the devices this project tests — see `useManualScan.setRadio`.
  'radio', 'radioTarget',
  'failure',
] as const

export type RunStatusKey = (typeof RUN_STATUS_KEYS)[number]
export type RunStatusFacts = Record<RunStatusKey, string | number>

/** `none` is the projection's word for "this scene has no such value". */
export const RUN_STATUS_NONE = 'none'
/** A declared value the App cannot honour is reported as itself, never guessed. */
export const RUN_STATUS_UNKNOWN = 'unknown'

const SEPARATOR = ';'

/**
 * The identity of one rendered failure: `platform/code` when the platform reported one,
 * else `domain/code`, else `none`.
 *
 * `scanFailureText` renders `domain/code: message [platform/code name: message]`, so the
 * platform answer is already in the text this projection is derived from. Reading it back
 * keeps one source of truth: the sentence stays the human view, and the identity — the
 * only part a scene may assert — becomes a pinned scalar instead of unbounded prose.
 */
function failureIdentity(text: string): string {
  if (text.trim() === '') return RUN_STATUS_NONE
  // A platform detail is more specific only when it names a *code*: the renderer writes
  // `[<platform>/<code> <component>]` then. Backends that report a component without a code
  // print `[<platform> <component>]`, and reading that as `android/not` threw away the one
  // fact a scene can assert (`not_supported`) — measured on `adapter-control`, 2026-11-26.
  const platform = /\[([^/\]\s:]+)\/([^\]\s:]+)/.exec(text)
  if (platform) return `${platform[1].toLowerCase()}/${platform[2]}`
  const domain = /^([a-z]+)\/([a-z0-9_.]+)/.exec(text)
  return domain ? `${domain[1]}/${domain[2]}` : RUN_STATUS_UNKNOWN
}

/**
 * One value, with the separators removed.
 *
 * Every value here is an enum, a count, a UUID list or a row key, so the replacement
 * never fires for a valid fact. It exists so a malformed one cannot make the whole
 * projection unparsable: a scene then fails on the value it expected, not on the frame.
 */
const field = (value: string): string => value.replace(/[;\r\n]+/g, ' ').trim()

export function encodeRunStatus(facts: RunStatusFacts): string {
  return RUN_STATUS_KEYS.map(key => `${key}=${field(String(facts[key]))}`).join(SEPARATOR)
}

export interface RunStatusInput {
  /** Session lifecycle state, as the status card shows it. */
  state: string
  /** Selected workbench page. */
  page: string
  view: {
    phase: string
    link: string
    /**
     * What ended the link: `none` until an end is observed, then `requested`, `unrequested` or
     * `owner`. A scene asserts the difference between an end it caused and one nobody here asked
     * for, and the human sentence for either is free form.
     */
    linkEnd: string
    gattPhase: string
    /** The read lane's phase: `none` | `waiting` | `cancelled` | `done` | `failed`. */
    gattRead: string
    notifyPhase: string
    notifyMode?: string
    total: number
    notifyTotal: number
    /** Link-fact lanes (`none|pending|done|unsupported|failed`), see `RUN_STATUS_KEYS`. */
    mtuPhase: string
    /** `<withResponse>/<withoutResponse>`, or absent when the platform reported neither. */
    mtuValue?: string
    mtuRequestPhase: string
    priorityPhase: string
    refreshPhase: string
    descriptorPhase: string
    descriptorValue?: string
    descriptorWritePhase: string
    /** The `connected` inventory query's terminal (`none|pending|done|unsupported|failed`). */
    deviceQueryPhase: string
    /** Entries the completed inventory returned, or absent before one. */
    deviceCount?: number
    /** Whether that inventory contained the peer this run is linked to. */
    devicePeer?: boolean
    /** Which inventory the completed query asked for: `connected`, `bonded` or `companion`. */
    deviceKind?: string
    /** The auto-connect lane's terminal (`none|pending|done|unsupported|failed`). */
    autoPhase: string
    /** The radio-control lane's terminal, same vocabulary. */
    radioPhase: string
    /** Which direction the last radio request asked for: `off` or `on`. */
    radioTarget?: string
  }
  /** Raw observation-timeout input; the projection reports whether it is usable. */
  timeoutText: string
  /** Raw service-filter input; the projection reports its canonical form. */
  filterText: string
  /** Characteristic UUID the GATT action strip acts on, as the caller declared it. */
  gattTarget: string
  /** `none` | `invalid` | `ambiguous` | `one`: how that declaration resolved. */
  gattMatch: string
  /** The resolved row's platform-reported capabilities, or `none`. */
  gattProps: string
  /** Read waits outstanding right now; a second read is queued, not rejected. */
  gattReads: number
  /**
   * Read waits this run has settled, any terminal (`done`/`failed`/`cancelled`).
   *
   * The outstanding count returns to zero after every read, so two sequential reads are
   * indistinguishable by it — and the App keeps showing the previous read's value, so the
   * rendered text cannot tell them apart either. A scene that submits a second read waits
   * for this count to advance instead.
   */
  gattReadTotal: number
  /**
   * Descriptor reads this run has settled, any terminal.
   *
   * Like `gattReadTotal`, this is the only way to tell two sequential reads apart: the App
   * keeps showing the previous read's value until the next settles, so a scene whose
   * expected value already equals that value would otherwise need no read at all.
   */
  descriptorReadTotal: number
  /**
   * The failure the run is showing, as `<platform>/<platform code>` when the platform
   * reported one, else `<domain>/<code>`, else `none`.
   *
   * A scene has to assert *which* failure happened, and the human sentence for it is free
   * form and unbounded. Only an identity belongs in the projection. Absent means the run
   * shows no failure.
   */
  failureText?: string
}

/**
 * Build the facts from the same render inputs the visible panels use.
 *
 * The projection is a second *presentation* of the run's one view state, not a second
 * source of truth: it is derived in the same render pass, so it cannot disagree with
 * the labelled elements a human reads.
 */
export function runStatusFacts(input: RunStatusInput): RunStatusFacts {
  const { view } = input
  const filter = parseScanFilter(input.filterText)
  const timeout = /^[0-9]+$/.test(input.timeoutText.trim())
    ? input.timeoutText.trim()
    : RUN_STATUS_UNKNOWN
  const declared = input.gattTarget.trim().toLowerCase()
  return {
    state: input.state,
    page: input.page,
    scan: view.phase,
    link: view.link,
    linkEnd: view.linkEnd,
    gatt: view.gattPhase,
    notify: view.notifyPhase,
    scanTotal: view.total,
    notifyTotal: view.notifyTotal,
    // A subscription exists but cannot report its mode: that is `unknown`, never `none`.
    notifyMode: view.notifyMode
      ?? (view.notifyPhase === 'none' ? RUN_STATUS_NONE : RUN_STATUS_UNKNOWN),
    // A cancelled read is a terminal the operator requested; a scene asserts that, not
    // "no result yet", which is why the phase is a scalar rather than a free-form line.
    gattRead: view.gattRead,
    gattReads: input.gattReads,
    gattReadTotal: input.gattReadTotal,
    mtu: view.mtuPhase,
    // The portable half of an MTU answer: CoreBluetooth reports no negotiated MTU at all, so
    // a scene can only require that the write limits are present and self-consistent.
    mtuWrites: view.mtuValue ?? RUN_STATUS_NONE,
    mtuRequest: view.mtuRequestPhase,
    priority: view.priorityPhase,
    refresh: view.refreshPhase,
    descriptor: view.descriptorPhase,
    // The peer's own bytes, so a scene can assert the CCCD state instead of trusting the App.
    descriptorValue: view.descriptorValue ?? RUN_STATUS_NONE,
    descriptorWrite: view.descriptorWritePhase,
    descriptorReadTotal: input.descriptorReadTotal,
    devices: view.deviceQueryPhase,
    devicesCount: view.deviceCount ?? RUN_STATUS_NONE,
    devicesPeer: view.devicePeer === undefined
      ? RUN_STATUS_NONE
      : view.devicePeer ? 'yes' : 'no',
    devicesKind: view.deviceKind ?? RUN_STATUS_NONE,
    auto: view.autoPhase,
    radio: view.radioPhase,
    radioTarget: view.radioTarget ?? RUN_STATUS_NONE,
    scanTimeout: timeout,
    scanFilter: filter.error !== undefined
      ? RUN_STATUS_UNKNOWN
      : filter.uuids.length === 0 ? RUN_STATUS_NONE : filter.uuids.join(','),
    gattTarget: declared === '' ? RUN_STATUS_NONE : declared,
    gattMatch: input.gattMatch,
    gattProps: input.gattProps,
    failure: failureIdentity(input.failureText ?? ''),
  }
}
