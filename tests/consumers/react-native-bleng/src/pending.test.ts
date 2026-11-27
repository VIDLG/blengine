import type {
  AbortSignalLike, BlengReceipt, BlengFailure, BlengFailureInfo, BlengOperationError as RaisedOperationError,
  BlengInitializeOutcome, BlengInitializeResult,
  BlengRead, BlengReadOutcome, BlengReadResult, BlengReadSubmission, BlengScan,
  BlengScanSubmission, BlengWrite, BlengWriteSubmission,
} from '@blengine/react-native-bleng'

// This file is about a pure-JS layer, so the two native boundaries are stubbed the same way
// `EngineBinding.test.ts` does it: the real package source is loaded, nothing native is.
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
const packageSourceDirectory = dirname(resolveModule('@blengine/react-native-bleng'))
for (const directory of [consumerDirectory, packageSourceDirectory]) {
  jest.doMock(resolveModule('react-native', { paths: [directory] }), () => reactNativeMock)
  jest.doMock(resolveModule('react-native-nitro-modules', { paths: [directory] }), () => nitroMock)
}
const { BlengOperationError, pendingOperation, run, isBlengFailure, requireAccepted, requireOutcome } =
  jest.requireActual<typeof import('@blengine/react-native-bleng')>('@blengine/react-native-bleng')

/**
 * The async/await layer is a *derived* surface: it may only submit through `begin*`, settle
 * through that operation's `wait`, and cancel through an explicit `cancel()`. These tests pin
 * exactly that, because every way of getting it wrong is a silent behaviour change:
 * dropping a promise must never cancel, and a failure must never be re-worded.
 */

const cancelled: BlengFailureInfo = {
  domain: 'operation',
  code: 'bleng.cancelled',
  message: 'BLeng operation was cancelled',
}
const unreadable: BlengFailureInfo = {
  domain: 'input',
  code: 'bleng.invalid_request.characteristic_not_readable',
  message: 'the characteristic is not readable',
}
const refusal: BlengFailure = { failure: unreadable }

function readCapability() {
  return {
    wait: jest.fn<Promise<BlengReadResult>, [number]>(),
    cancel: jest.fn<Promise<BlengReceipt>, []>(),
  }
}

/** One accepted submission, typed exactly as a caller would receive it from `beginRead()`. */
function submission(read: ReturnType<typeof readCapability>) {
  return { read: read as unknown as BlengRead }
}

/** A minimal, fully controllable `AbortSignal`; a real `AbortController.signal` fits the same shape. */
function fakeSignal(alreadyAborted = false) {
  const listeners = new Set<() => void>()
  const addEventListener = jest.fn((_type: 'abort', listener: () => void) => {
    listeners.add(listener)
  })
  const removeEventListener = jest.fn((_type: 'abort', listener: () => void) => {
    listeners.delete(listener)
  })
  const signal: AbortSignalLike = { aborted: alreadyAborted, addEventListener, removeEventListener }
  return {
    signal,
    addEventListener,
    removeEventListener,
    abort() {
      for (const listener of [...listeners]) listener()
      listeners.clear()
    },
  }
}

describe('pendingOperation', () => {
  it('settles with the value the operation waited for, on the declared budget', async () => {
    const read = readCapability()
    read.wait.mockResolvedValue({ value: new Uint8Array([7]).buffer })

    const pending = pendingOperation(submission(read), 5_000)

    expect(read.wait).toHaveBeenCalledWith(5_000)
    await expect(pending.result).resolves.toEqual({ value: new Uint8Array([7]).buffer })
  })

  it('raises a refused submission synchronously, before anything is submitted', () => {
    const read = readCapability()

    expect(() => pendingOperation(refusal, 5_000)).toThrow(BlengOperationError)
    expect(read.wait).not.toHaveBeenCalled()
  })

  it('carries the native failure unchanged, with `code` as the control-flow field', () => {
    let raised: unknown
    try {
      pendingOperation(refusal, 5_000)
    } catch (error) {
      raised = error
    }

    expect(raised).toBeInstanceOf(BlengOperationError)
    const failure = (raised as RaisedOperationError).failure
    expect(failure).toBe(unreadable)
    expect((raised as RaisedOperationError).code).toBe(unreadable.code)
    expect((raised as RaisedOperationError).domain).toBe('input')
    expect((raised as RaisedOperationError).message).toBe(unreadable.message)
  })

  it('rejects with the settled failure instead of resolving a failure record', async () => {
    const read = readCapability()
    read.wait.mockResolvedValue({ failure: cancelled })

    const pending = pendingOperation(submission(read), 1_000)

    await expect(pending.result).rejects.toBeInstanceOf(BlengOperationError)
    await expect(pending.result).rejects.toMatchObject({ code: 'bleng.cancelled' })
  })

  it('does not cancel when the result promise is dropped', async () => {
    const read = readCapability()
    read.wait.mockReturnValue(new Promise(() => {}))

    const pending = pendingOperation(submission(read), 30_000)
    void pending.result

    expect(read.cancel).not.toHaveBeenCalled()
  })

  it('submits exactly one cancel, and hands its receipt back unchanged', async () => {
    const read = readCapability()
    read.wait.mockReturnValue(new Promise(() => {}))
    const receipt: BlengReceipt = { failure: cancelled }
    read.cancel.mockResolvedValue(receipt)

    const pending = pendingOperation(submission(read), 30_000)
    await expect(pending.cancel()).resolves.toBe(receipt)
    expect(read.cancel).toHaveBeenCalledTimes(1)
  })

  it('keeps two pending operations independent', async () => {
    const first = readCapability()
    const second = readCapability()
    first.wait.mockResolvedValue({ failure: cancelled })
    second.wait.mockResolvedValue({ value: new Uint8Array([1]).buffer })

    const one = pendingOperation(submission(first), 100)
    const two = pendingOperation(submission(second), 200)

    await expect(one.result).rejects.toMatchObject({ code: 'bleng.cancelled' })
    await expect(two.result).resolves.toEqual({ value: new Uint8Array([1]).buffer })
    expect(first.cancel).not.toHaveBeenCalled()
    expect(second.cancel).not.toHaveBeenCalled()
  })

  it('accepts any operation shape the bridge exposes, not only reads', async () => {
    const write = {
      wait: jest.fn<Promise<BlengReceipt>, [number]>(),
      cancel: jest.fn<Promise<BlengReceipt>, []>(),
    }
    write.wait.mockResolvedValue({})
    const submitted: BlengWriteSubmission = { write: write as unknown as BlengWrite }

    await expect(pendingOperation(submitted, 750).result).resolves.toEqual({})
    expect(write.wait).toHaveBeenCalledWith(750)
  })

  it('awaits a lane-named control only through an explicit adapter', async () => {
    // `beginScan()` and `beginSubscription()` name their own controls, so the layer
    // never renames them: the adapter is written at the call site and stays visible.
    const waitUntilStarted = jest
      .fn<Promise<BlengReceipt>, [number]>()
      .mockResolvedValue({})
    const cancelStart = jest.fn<Promise<BlengReceipt>, []>().mockResolvedValue({})

    const pending = pendingOperation(
      {
        operation: {
          wait: (ms: number) => waitUntilStarted(ms),
          cancel: () => cancelStart(),
        },
      },
      5_000,
    )

    await expect(pending.result).resolves.toEqual({})
    expect(waitUntilStarted).toHaveBeenCalledWith(5_000)
    await pending.cancel()
    expect(cancelStart).toHaveBeenCalledTimes(1)
  })

  it('types the settled value from the submission, with no lane list to maintain', async () => {
    const read = readCapability()
    read.wait.mockResolvedValue({ value: new ArrayBuffer(1) })
    const submitted: BlengReadSubmission = submission(read)

    // Compile-time assertion: the settled arm, not the submission union, and not the failure arm.
    const settled: Promise<BlengReadOutcome> = pendingOperation(submitted, 100).result

    await expect(settled).resolves.toEqual({ value: new ArrayBuffer(1) })
  })

  it('aborting submits the operation cancel but leaves the result to native', async () => {
    const read = readCapability()
    read.cancel.mockResolvedValue({})
    let finish!: (value: BlengReadResult) => void
    read.wait.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const controller = fakeSignal()

    const pending = pendingOperation(submission(read), 5_000, controller.signal)
    controller.abort()

    expect(read.cancel).toHaveBeenCalledTimes(1)
    // Abort is a request, not rollback: `result` still settles from the native outcome.
    finish({ value: new Uint8Array([9]).buffer })
    await expect(pending.result).resolves.toEqual({ value: new Uint8Array([9]).buffer })
    await Promise.resolve()
    expect(controller.removeEventListener).toHaveBeenCalledTimes(1)
  })

  it('submits cancel immediately for an already-aborted signal', () => {
    const read = readCapability()
    read.cancel.mockResolvedValue({})
    read.wait.mockReturnValue(new Promise(() => {}))
    const controller = fakeSignal(true)

    pendingOperation(submission(read), 5_000, controller.signal)

    expect(read.cancel).toHaveBeenCalledTimes(1)
    expect(controller.addEventListener).not.toHaveBeenCalled()
  })

  it('detaches the abort listener once the operation settles', async () => {
    const read = readCapability()
    read.wait.mockResolvedValue({ value: new ArrayBuffer(1) })
    const controller = fakeSignal()

    const pending = pendingOperation(submission(read), 5_000, controller.signal)
    await pending.result
    await Promise.resolve()

    expect(controller.removeEventListener).toHaveBeenCalledTimes(1)
    expect(read.cancel).not.toHaveBeenCalled()
  })

  it('refuses a budget that is not a finite positive number', () => {
    const read = readCapability()
    for (const budget of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => pendingOperation(submission(read), budget)).toThrow(RangeError)
    }
    expect(read.wait).not.toHaveBeenCalled()
  })

  it('refuses a submission that does not carry exactly one capability', () => {
    const read = readCapability()
    expect(() => pendingOperation({}, 100)).toThrow(TypeError)
    expect(() =>
      pendingOperation({ scan: { nextEvent: jest.fn() } as unknown as BlengScan }, 100),
    ).toThrow(TypeError)
    expect(() =>
      pendingOperation({ read: read as unknown as BlengRead, again: read as unknown as BlengRead }, 100),
    ).toThrow(TypeError)
    expect(read.wait).not.toHaveBeenCalled()
  })
})

describe('requireAccepted', () => {
  it('unwraps a lane-yielding submission without renaming its controls', () => {
    // `beginScan()` hands back a lane, not one operation; this removes the union check and
    // nothing else, so `waitUntilStarted`/`cancelStart` stay the scan lane's own words.
    const scan = { waitUntilStarted: jest.fn(), cancelStart: jest.fn() }
    const submitted = { scan } as unknown as BlengScanSubmission

    expect(requireAccepted(submitted)).toBe(scan as unknown as BlengScan)
    expect(() => requireAccepted(refusal)).toThrow(BlengOperationError)
  })
})

describe('run', () => {
  it('is the one-liner: settles the outcome with the declared budget', async () => {
    const read = readCapability()
    read.wait.mockResolvedValue({ value: new Uint8Array([1]).buffer })

    await expect(run(submission(read), 1_000)).resolves.toEqual({ value: new Uint8Array([1]).buffer })
    expect(read.wait).toHaveBeenCalledWith(1_000)
  })

  it('throws for a refused submission and rejects for a settled failure', async () => {
    expect(() => run(refusal, 1_000)).toThrow(BlengOperationError)

    const read = readCapability()
    read.wait.mockResolvedValue({ failure: unreadable })
    await expect(run(submission(read), 1_000)).rejects.toBeInstanceOf(BlengOperationError)
  })
})

describe('requireOutcome', () => {
  it('returns the outcome arm of a successful start', () => {
    const outcome = {
      snapshot: { state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized' },
    } as BlengInitializeOutcome
    expect(requireOutcome(outcome)).toBe(outcome)
  })

  it('raises a native start failure carried as data', () => {
    const refused: BlengInitializeResult = { failure: unreadable }
    expect(() => requireOutcome(refused)).toThrow(BlengOperationError)
    let raised: unknown
    try {
      requireOutcome(refused)
    } catch (error) {
      raised = error
    }
    expect((raised as RaisedOperationError).failure).toBe(unreadable)
  })
})

describe('isBlengFailure', () => {
  it('recognises the one failure shape and nothing else', () => {
    expect(isBlengFailure({ failure: unreadable })).toBe(true)
    expect(isBlengFailure({ value: new Uint8Array([0]).buffer })).toBe(false)
    expect(isBlengFailure({})).toBe(false)
    expect(isBlengFailure({ failure: null })).toBe(false)
    expect(isBlengFailure(undefined)).toBe(false)
    expect(isBlengFailure(7)).toBe(false)
  })

  it('narrows on the closed failure domain, not on a bare `failure` key', () => {
    for (const domain of ['input', 'submission', 'operation', 'observation', 'delivery', 'lifecycle']) {
      expect(isBlengFailure({ failure: { domain, code: 'x', message: 'y' } })).toBe(true)
    }
    // A foreign record that merely carries a `failure` object must not be mistaken for one.
    expect(isBlengFailure({ failure: { domain: 'not-a-domain', code: 'x', message: 'y' } })).toBe(false)
    expect(isBlengFailure({ failure: { code: 'x', message: 'y' } })).toBe(false)
    expect(isBlengFailure({ failure: { domain: 'operation', code: 7, message: 'y' } })).toBe(false)
    expect(isBlengFailure({ failure: { domain: 'operation', code: 'x', message: 7 } })).toBe(false)
    expect(isBlengFailure({ failure: { domain: 'operation', code: 'x' } })).toBe(false)
  })
})
