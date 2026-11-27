/**
 * The run's own action ledger, as machine data.
 *
 * `lifecycle-evidence` is the human view: prose lines, inside the scroll container, and
 * therefore not something a scene may read (Android drops off-viewport children, and a
 * swipe is only as accurate as input injection). A scene still has to establish that the
 * App recorded the actions it dispatched, so the same bounded list is published here as
 * one pinned node in a strict, unambiguous form:
 *
 * ```text
 * omitted=0
 * 1|start|succeeded|
 * 2|begin-scan|succeeded|
 * 3|notify-pull|succeeded|ended: disabled
 * ```
 *
 * - line 0 is `omitted=<count of entries the bounded window dropped>`;
 * - each further line is `sequence|action|result|detail`;
 * - `|` and newlines are stripped from every value, so one entry can never become two;
 * - the list keeps the App's own bound (`EVIDENCE_LIMIT`), so the node stays one screen.
 *
 * The decoder is `tools/react_native/action_ledger.py`, and the contract is stated in
 * `tests/consumers/README.md`. Both sides declare the same shape: an unparsable line, a
 * non-contiguous sequence or a foreign action is drift and must fail the scene rather
 * than be ignored.
 */

/**
 * One recorded action, in the order the App committed it.
 *
 * `error` is the App's own field for an action's outcome sentence (`Observation` in
 * `App.tsx`), and it is what the frame's `detail` segment carries. Naming it `detail` here
 * once dropped every failure sentence from the machine view: the encoder read a field the
 * recorder never wrote, so a cancelled wait rendered as `failed|` with nothing after it.
 */
export interface ActionLedgerEntry {
  sequence: number
  action: string
  result: string
  error?: string
}

const SEPARATOR = '|'
const HEADER = 'omitted='

/** One value, with the separators removed so the frame cannot be ambiguous. */
const field = (value: string): string => value.replace(/[|\r\n]+/g, ' ').trim()

export function encodeActionLedger(
  entries: readonly ActionLedgerEntry[],
  omitted: number,
): string {
  const lines = [`${HEADER}${Math.max(0, Math.trunc(omitted))}`]
  for (const entry of entries) {
    lines.push([
      String(entry.sequence),
      field(entry.action),
      field(entry.result),
      field(entry.error ?? ''),
    ].join(SEPARATOR))
  }
  return lines.join('\n')
}
