package dev.blehub.android

/** Apply the contract service filter after Android has delivered a scan result. */
internal fun matchesRequestedServiceUuids(
    requested: Iterable<String>,
    advertised: Iterable<String>,
): Boolean {
    val advertisedValues = advertised.map(String::lowercase).toSet()
    return requested.any { it.lowercase() in advertisedValues }
}
