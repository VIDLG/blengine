package dev.blehub.android

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class ScanServiceFilterTest {
    @Test
    fun matchesAnyRequestedServiceWithoutDependingOnUuidCase() {
        assertTrue(
            matchesRequestedServiceUuids(
                requested = listOf("FD200000-0000-4000-8000-000000000000"),
                advertised = listOf("fd200000-0000-4000-8000-000000000000"),
            ),
        )
        assertTrue(
            matchesRequestedServiceUuids(
                requested = listOf("fd000000-0000-4000-8000-000000000000", "fd200000-0000-4000-8000-000000000000"),
                advertised = listOf("fd200000-0000-4000-8000-000000000000"),
            ),
        )
    }

    @Test
    fun rejectsResultsWithoutARequestedService() {
        assertFalse(
            matchesRequestedServiceUuids(
                requested = listOf("fd200000-0000-4000-8000-000000000000"),
                advertised = listOf("fd000000-0000-4000-8000-000000000000"),
            ),
        )
        assertFalse(
            matchesRequestedServiceUuids(
                requested = emptyList(),
                advertised = listOf("fd200000-0000-4000-8000-000000000000"),
            ),
        )
    }
}
