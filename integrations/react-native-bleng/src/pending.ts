import type { BlengFailure, BlengFailureInfo, BlengReceipt } from '../specs/BlengFailure.nitro'
import { failureReason } from './reason'

/**
 * `begin*` / `wait` / `cancel` are the submission model; this file is a convenience layer *on
 * top of* it, never a second one.
 *
 * The rules that keep it derived rather than parallel:
 *
 * - it can only be built from what a `begin*` returned, and it settles by calling that
 *   operation's own bounded wait;
 * - cancellation stays explicit. Dropping the `result` promise is not a cancellation and never
 *   becomes one — only `pending.cancel()` submits a cancel, and its receipt is handed back
 *   unchanged, because a cancel receipt records a request, not rollback;
 * - failures are never re-worded: a refused submission and a settled failure both arrive as
 *   `BlengOperationError` carrying the native `BlengFailureInfo`, whose `code` is the only field
 *   safe for control flow (`message` is display only).
 *
 * **Two lanes are deliberately not covered by the submission overloads.** `beginScan()` and
 * `beginSubscription()` expose lane-named controls (`waitUntilStarted`/`cancelStart`,
 * `waitUntilEnabled`) plus a stream that is not an operation (`nextEvent`), and renaming those
 * to a uniform `wait`/`cancel` would hide which lane a caller is on. They use the explicit form
 * instead, so the adapter is visible at the call site:
 *
 * ```ts
 * const started = pendingOperation<BlengReceipt>(
 *   { operation: { wait: (ms: number) => scan.waitUntilStarted(ms), cancel: () => scan.cancelStart() } },
 *   5_000,
 * )
 * ```
 *
 * **Three shapes, three helpers — each maps to a different wire shape.**
 *
 * - an *operation* submission (one capability with `wait`/`cancel`) → `pendingOperation(...)` →
 *   `{ result, cancel }`; this is the bounded-submit + settlement + cancel path;
 * - a *lane* submission (named controls plus a stream) → `requireAccepted(...)` → the capability;
 * - a *settled result* (`initialize()`, or any operation's `wait` value) → `isBlengFailure` to branch,
 *   or `requireOutcome(...)` to return the outcome arm or throw.
 *
 * `requireAccepted` is the explicit unwrap: required for the two lanes, and also valid for an
 * operation when you want to call its own `wait`/`cancel` yourself. `pendingOperation` is the
 * bundled operation convenience (it starts the bounded wait and keeps the cancel handle).
 */

/** The two controls every accepted single-shot operation exposes. */
export interface Operation<TValue> {
  wait(timeoutMs: number): Promise<TValue>
  cancel(): Promise<BlengReceipt>
}

/** What a `begin*` returned: either one capability or the failure that refused it. */
export type OperationSubmission<TValue> = BlengFailure | Record<string, Operation<TValue>>

/**
 * The settled success arm of one submission, derived from the submission type itself.
 *
 * A capability that exposes `wait`/`cancel` yields `wait`'s value minus the failure arm (that arm
 * is thrown, never resolved). A capability that does *not* — `beginScan()` and
 * `beginSubscription()`, which name their own controls — yields `never`, so the excluded
 * lanes are refused at compile time instead of being silently accepted by a hand-maintained list.
 */
type SettledOf<TSubmission> = {
  [TKey in keyof TSubmission]: TSubmission[TKey] extends Operation<infer TValue>
    ? Exclude<TValue, BlengFailure>
    : never
}[keyof TSubmission]

/** The one capability a submission carries, with its refusal removed. */
type AcceptedOf<TSubmission> = Exclude<TSubmission, BlengFailure>[keyof Exclude<
  TSubmission,
  BlengFailure
>]

/**
 * Unwrap one `begin*` result, raising its refusal.
 *
 * This is the escape hatch for the two lanes whose submission yields a *lane* rather than one
 * operation: `beginScan()` and `beginSubscription()`. It removes the union check without
 * renaming those lanes' own controls.
 *
 * ```ts
 * const scan = requireAccepted(central.beginScan({ serviceUuids: [], allowDuplicates: false }))
 * await scan.waitUntilStarted(5_000)
 * ```
 */
export function requireAccepted<TSubmission extends object>(
  submission: TSubmission,
): AcceptedOf<TSubmission> {
  return singleCapability(submission) as AcceptedOf<TSubmission>
}

/**
 * Unwrap one settled result union, raising its failure and returning the success arm.
 *
 * A `*Result` is `*Outcome | BlengFailure` (for example `BlengInitializeResult`, or any operation's
 * `wait` value). This is the throwing view of that union, built on the one `isBlengFailure` predicate
 * and the one `BlengOperationError`; it introduces no second result type.
 *
 * It returns the **outcome arm** (e.g. `{ snapshot }`) rather than an auto-extracted field, because
 * receipts are legitimately `{}` and an outcome may gain fields. Use it only on a two-arm result
 * union; a lane's `nextEvent` value has an extra `ended` arm and must be branched on.
 *
 * ```ts
 * const { snapshot } = requireOutcome(await central.initialize())
 * ```
 */
export function requireOutcome<TResult extends object>(
  result: TResult,
): Exclude<TResult, BlengFailure> {
  if (isBlengFailure(result)) {
    throw new BlengOperationError(result.failure)
  }
  return result as Exclude<TResult, BlengFailure>
}

/** One accepted operation, kept pending on purpose: the caller decides when to await or cancel. */
export interface PendingOperation<TValue> {
  /** The settled outcome/receipt, or a rejection carrying the native failure. */
  readonly result: Promise<TValue>
  /** Submit an explicit cancel; its receipt is returned as native reported it. */
  cancel(): Promise<BlengReceipt>
}

/**
 * The subset of the standard `AbortSignal` this package binds to.
 *
 * A DOM/RN `AbortSignal` (from `new AbortController().signal`) is structurally assignable, so
 * callers pass it unchanged; this package does not depend on DOM lib types just to name it.
 */
export interface AbortSignalLike {
  readonly aborted: boolean
  addEventListener(type: 'abort', listener: () => void, options?: { once?: boolean }): void
  removeEventListener(type: 'abort', listener: () => void): void
}

/** A native failure, raised rather than returned so `await` and `try` are one code path. */
export class BlengOperationError extends Error {
  readonly failure: BlengFailureInfo

  constructor(failure: BlengFailureInfo) {
    super(failure.message)
    this.name = 'BlengOperationError'
    this.failure = failure
  }

  /** The only field safe for control flow. */
  get code(): string {
    return this.failure.code
  }

  /** Stable English sentence for display/logs; never use it for control flow. */
  get reason(): string {
    return failureReason(this.failure)
  }

  get domain(): BlengFailureInfo['domain'] {
    return this.failure.domain
  }
}

/**
 * The closed set Rust uses to say where a failure was observed.
 *
 * `BlengFailureDomain` is a literal union, so checking `failure.domain` against this set is a real
 * tagged check, not shape sniffing: a foreign object that merely happens to carry a `failure` key
 * is not mistaken for a native failure.
 */
const FAILURE_DOMAINS: ReadonlySet<string> = new Set<string>([
  'input',
  'submission',
  'operation',
  'observation',
  'delivery',
  'lifecycle',
])

function isFailureInfo(value: unknown): value is BlengFailureInfo {
  if (value === null || typeof value !== 'object') {
    return false
  }
  const info = value as Partial<BlengFailureInfo>
  return (
    typeof info.domain === 'string'
    && FAILURE_DOMAINS.has(info.domain)
    && typeof info.code === 'string'
    && typeof info.message === 'string'
  )
}

/**
 * True for the one record shape every BLeng view uses to mean "this did not succeed".
 *
 * Narrow with this guard rather than `switch`/`in` on the union: the generated success arms carry
 * no mirrored tag, and `failure.domain` is already a closed literal discriminant. `null`, a bare
 * `{ failure }`, a non-string `code`/`message`, or an unknown `domain` is not a native failure.
 */
export function isBlengFailure(value: unknown): value is BlengFailure {
  if (value === null || typeof value !== 'object') {
    return false
  }
  return isFailureInfo((value as { failure?: unknown }).failure)
}

/**
 * Accept one `begin*` result and describe how to await it.
 *
 * A refused submission is a synchronous fact, so it throws here instead of returning a promise
 * that would reject before anyone could attach a handler. Everything after that is asynchronous.
 *
 * The accepted shape is structural: any submission whose capability has `wait`/`cancel` works,
 * and no list of lane names has to be maintained as the bridge grows.
 *
 * `signal` is an optional, idiomatic cancellation binding: aborting it submits this operation's
 * own native `cancel()`. It is a **request**, not local rollback, so `result` still settles from
 * the native outcome (a native cancellation usually arrives as a rejected `BlengOperationError`).
 * The signal never becomes a second cancel path: `cancel()` and the signal submit the same call.
 */
export function pendingOperation<TSubmission extends object>(
  submission: TSubmission,
  timeoutMs: number,
  signal?: AbortSignalLike,
): PendingOperation<SettledOf<Exclude<TSubmission, BlengFailure>>>
export function pendingOperation(
  submission: object,
  timeoutMs: number,
  signal?: AbortSignalLike,
): PendingOperation<unknown> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('timeoutMs must be a finite positive budget')
  }
  const operation = singleCapability(submission) as Operation<unknown>
  if (typeof operation.wait !== 'function' || typeof operation.cancel !== 'function') {
    throw new TypeError('pendingOperation expects an operation submission with wait/cancel, not a lane')
  }
  const cancel = (): Promise<BlengReceipt> => operation.cancel()
  const result = settle(operation, timeoutMs)
  if (signal) {
    bindAbort(signal, cancel, result)
  }
  return { result, cancel }
}

/**
 * Run one operation to completion: the one-liner.
 *
 * This is exactly `pendingOperation(submission, timeoutMs, signal).result`: it waits with your
 * bounded budget and returns the outcome, or throws `BlengOperationError` for a refused submission
 * or a settled failure. It deliberately exposes **no** cancel handle — if you may need to cancel,
 * call `pendingOperation` and keep its `cancel()` (or pass a signal).
 *
 * ```ts
 * const { connection } = await run(peripheral.beginConnect(), 10_000)
 * ```
 */
export function run<TSubmission extends object>(
  submission: TSubmission,
  timeoutMs: number,
  signal?: AbortSignalLike,
): Promise<SettledOf<Exclude<TSubmission, BlengFailure>>> {
  return pendingOperation(submission, timeoutMs, signal).result
}

/**
 * Bind one `AbortSignal` to this operation's native cancel, and detach when the operation settles.
 *
 * An already-aborted signal submits the cancel immediately (the operation was already admitted by
 * its `begin*`); a later abort submits once because the listener is `{ once: true }`.
 */
function bindAbort(
  signal: AbortSignalLike,
  cancel: () => Promise<BlengReceipt>,
  result: Promise<unknown>,
): void {
  const abort = (): void => {
    // The receipt is deliberately not the wait result; a rejected cancel is not surfaced here.
    void cancel().catch(() => {})
  }
  if (signal.aborted) {
    abort()
    return
  }
  signal.addEventListener('abort', abort, { once: true })
  const detach = (): void => signal.removeEventListener('abort', abort)
  result.then(detach, detach)
}

/** The one capability a submission carries, or the refusal it carries instead. */
function singleCapability(submission: object): unknown {
  if (isBlengFailure(submission)) {
    throw new BlengOperationError(submission.failure)
  }
  const capabilities = Object.values(submission)
  if (capabilities.length !== 1) {
    throw new TypeError('a begin() submission carries exactly one acceptance capability')
  }
  return capabilities[0]
}

async function settle<TValue>(
  operation: Operation<TValue>,
  timeoutMs: number,
): Promise<TValue> {
  const settled = await operation.wait(timeoutMs)
  if (isBlengFailure(settled)) {
    throw new BlengOperationError(settled.failure)
  }
  return settled
}
