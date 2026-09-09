package dev.blehub.ffi.runtime

import kotlin.test.assertTrue
import org.junit.Before

class UniFfiAndroidRuntimeTest : ProductionUniFfiRuntimeContract() {
    @Before
    fun requireAndroidRuntime() {
        val vmName = System.getProperty("java.vm.name").orEmpty()
        assertTrue(
            vmName.contains("dalvik", ignoreCase = true) ||
                    vmName.contains("art", ignoreCase = true),
            "expected Android runtime, got $vmName",
        )
    }
}
