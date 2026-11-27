package dev.blehub.android

/**
 * Apply the contract manufacturer-data predicate after Android has delivered a scan result.
 *
 * [payload] is the manufacturer-specific payload for one company id. It must start with [expected],
 * or match it byte-for-byte under [mask] when a mask of identical length is supplied. A shorter
 * payload never matches, mirroring the Apple predicate and the Rust contract validator.
 */
internal fun matchesManufacturerPayload(
    payload: ByteArray,
    expected: ByteArray,
    mask: ByteArray?,
): Boolean {
    if (payload.size < expected.size) return false
    if (mask == null) {
        return expected.indices.all { payload[it] == expected[it] }
    }
    if (mask.size != expected.size) return false
    return expected.indices.all { index ->
        (payload[index].toInt() and mask[index].toInt()) ==
                (expected[index].toInt() and mask[index].toInt())
    }
}
