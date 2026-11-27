package com.blehubblenghost

import com.margelo.nitro.blehubreactnativebleng.BlengAdapterSnapshot
import com.margelo.nitro.blehubreactnativebleng.BlengFailureInfo
import com.margelo.nitro.blehubreactnativebleng.BlengInitializeResult

/**
 * The snapshot of a successful start.
 *
 * `initialize()` reports a native failure as data (`Second`), not as a rejected Promise, exactly
 * like `close()` reports a cleanup failure. A test that needs a live session therefore has to treat
 * an unexpected failure as a test bug, which this does by throwing.
 */
internal fun BlengInitializeResult.snapshotOrThrow(): BlengAdapterSnapshot = when (this) {
    is BlengInitializeResult.First -> value.snapshot
    is BlengInitializeResult.Second -> throw AssertionError(
        "initialization failed unexpectedly: ${value.failure.code} (${value.failure.message})",
    )
}

/** The native failure of a refused start, or `null` when the start succeeded. */
internal fun BlengInitializeResult.failureOrNull(): BlengFailureInfo? =
    asSecondOrNull()?.failure
